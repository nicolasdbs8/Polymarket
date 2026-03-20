#!/usr/bin/env python3
"""
moteur.py — Moteur déterministe du système de paper trading.

Usage :
    python moteur.py --payload analysis_payload.json --config system_config.json
    python moteur.py --payload analysis_payload.json  # utilise system_config.json par défaut

Sortie :
    engine_output.json dans le répertoire courant (ou --output pour spécifier le chemin)
"""

import json
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(data: dict, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def level_to_value(mapping: dict, level: str, field_name: str) -> float:
    if level not in mapping:
        raise ValidationError(f"Valeur d'enum invalide pour '{field_name}': '{level}'. "
                              f"Valeurs attendues : {list(mapping.keys())}")
    return mapping[level]


# ─────────────────────────────────────────────────────────────────────────────
# Erreurs
# ─────────────────────────────────────────────────────────────────────────────

class ValidationError(Exception):
    """Erreur bloquante — le moteur refuse le calcul."""
    pass

class DegradationWarning:
    """Avertissement non bloquant — le calcul continue avec pénalité."""
    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message

    def __str__(self):
        return f"[DÉGRADATION] {self.field} : {self.message}"


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_payload(payload: dict, config: dict) -> list[DegradationWarning]:
    """
    Valide l'analysis_payload.
    - Lève ValidationError si un champ bloquant est invalide ou absent.
    - Retourne une liste de DegradationWarning pour les champs dégradants.
    """
    warnings = []

    # ── Champs racine obligatoires ────────────────────────────────────────────
    blocking_root = [
        "market_id", "thesis_id", "analysis_id", "analysis_timestamp",
        "protocol_version", "schema_version", "analysis_version",
        "screening", "base_rate", "prerequisites", "factor_list",
        "confidence", "ambiguity", "contradiction_forced"
    ]
    for field in blocking_root:
        if field not in payload:
            raise ValidationError(f"Champ obligatoire manquant : '{field}'")

    # ── base_rate ─────────────────────────────────────────────────────────────
    br = payload["base_rate"]
    if "base_rate_value" not in br:
        raise ValidationError("base_rate.base_rate_value est obligatoire (BLOQUANT).")
    val = br["base_rate_value"]
    if not (0.01 <= val <= 0.99):
        raise ValidationError(f"base_rate.base_rate_value hors bornes [0.01, 0.99] : {val}")

    if not br.get("base_rate_reference_class"):
        warnings.append(DegradationWarning(
            "base_rate.base_rate_reference_class",
            "Absent. La classe de référence historique n'est pas documentée."
        ))

    # ── factor_list ───────────────────────────────────────────────────────────
    fl = payload["factor_list"]
    if not fl:
        raise ValidationError("factor_list est vide (BLOQUANT).")

    for i, factor in enumerate(fl):
        for req in ["factor_name", "factor_type", "factor_score", "factor_weight"]:
            if req not in factor:
                raise ValidationError(f"factor_list[{i}].{req} est obligatoire (BLOQUANT).")

        score = factor["factor_score"]
        if score not in (-2, -1, 0, 1, 2):
            raise ValidationError(f"factor_list[{i}].factor_score invalide : {score}. Doit être dans [-2,-1,0,1,2].")

        weight = factor["factor_weight"]
        if weight not in (1, 2, 3):
            raise ValidationError(f"factor_list[{i}].factor_weight invalide : {weight}. Doit être dans [1,2,3].")

        if factor["factor_type"] not in ("accelerator", "brake"):
            raise ValidationError(f"factor_list[{i}].factor_type invalide : '{factor['factor_type']}'.")

        if weight == 3 and not factor.get("factor_comment"):
            warnings.append(DegradationWarning(
                f"factor_list[{i}].factor_comment",
                f"Absent sur le facteur de poids 3 '{factor['factor_name']}' (DÉGRADANT)."
            ))

    # ── confidence ────────────────────────────────────────────────────────────
    conf = payload["confidence"]
    for field in ["confidence_sources", "confidence_model", "confidence_context", "confidence_overall"]:
        if field not in conf:
            raise ValidationError(f"confidence.{field} est obligatoire (BLOQUANT).")
        if conf[field] not in ("high", "medium", "low"):
            raise ValidationError(f"confidence.{field} invalide : '{conf[field]}'.")

    # ── ambiguity ─────────────────────────────────────────────────────────────
    amb = payload["ambiguity"]
    for field in ["event_ambiguity", "resolution_ambiguity"]:
        if field not in amb:
            raise ValidationError(f"ambiguity.{field} est obligatoire (BLOQUANT).")
        if amb[field] not in ("high", "medium", "low"):
            raise ValidationError(f"ambiguity.{field} invalide : '{amb[field]}'.")

    # ── contradiction_forced ──────────────────────────────────────────────────
    cf = payload["contradiction_forced"]
    if not cf.get("best_counter_thesis"):
        raise ValidationError("contradiction_forced.best_counter_thesis est obligatoire (BLOQUANT).")

    reasons = cf.get("top_3_failure_reasons", [])
    if len(reasons) != 3:
        raise ValidationError(
            f"contradiction_forced.top_3_failure_reasons doit contenir exactement 3 éléments. "
            f"Reçu : {len(reasons)}."
        )

    if not cf.get("market_might_be_right_because"):
        warnings.append(DegradationWarning(
            "contradiction_forced.market_might_be_right_because",
            "Absent (DÉGRADANT)."
        ))
    if not cf.get("thesis_invalidation_trigger"):
        warnings.append(DegradationWarning(
            "contradiction_forced.thesis_invalidation_trigger",
            "Absent (DÉGRADANT)."
        ))

    # ── prerequisites ─────────────────────────────────────────────────────────
    prereqs = payload["prerequisites"]
    if "blocking" not in prereqs or "weighted" not in prereqs:
        raise ValidationError("prerequisites doit contenir 'blocking' et 'weighted'.")

    valid_statuses = ("filled", "partial", "not_filled", "unknown")
    for kind in ("blocking", "weighted"):
        for i, p in enumerate(prereqs[kind]):
            if "name" not in p or "status" not in p:
                raise ValidationError(f"prerequisites.{kind}[{i}] doit avoir 'name' et 'status'.")
            if p["status"] not in valid_statuses:
                raise ValidationError(
                    f"prerequisites.{kind}[{i}].status invalide : '{p['status']}'. "
                    f"Valeurs attendues : {valid_statuses}"
                )

    return warnings


# ─────────────────────────────────────────────────────────────────────────────
# Calcul de probabilité
# ─────────────────────────────────────────────────────────────────────────────

def compute_probability(payload: dict, config: dict) -> dict:
    """
    Pipeline d'estimation probabiliste :
    base_rate → veto prérequis bloquants → score facteurs → translation → fourchette
    """
    result = {}
    cfg = config

    base_rate = payload["base_rate"]["base_rate_value"]
    result["base_rate"] = base_rate

    # ── Étape 1 : veto prérequis bloquants ───────────────────────────────────
    blocking_prereqs = payload["prerequisites"]["blocking"]
    blocking_veto = any(p["status"] == "not_filled" for p in blocking_prereqs)
    result["blocking_veto_triggered"] = blocking_veto

    if blocking_veto:
        veto_p = round(base_rate * cfg["blocking_prerequisite_penalty"], 4)
        result["p_after_blocking_veto"] = veto_p
        result["blocking_veto_reason"] = "Prérequis bloquant non rempli."
    else:
        result["p_after_blocking_veto"] = None

    # ── Étape 2 : coefficient prérequis pondérants ───────────────────────────
    weighted_prereqs = payload["prerequisites"]["weighted"]
    pf_map = cfg["prerequisite_factors"]

    if weighted_prereqs:
        pf_values = [level_to_value(pf_map, p["status"], f"prerequisites.weighted[].status")
                     for p in weighted_prereqs]
        prerequisite_factor = round(sum(pf_values) / len(pf_values), 4)
    else:
        prerequisite_factor = 1.0

    result["prerequisite_factor"] = prerequisite_factor

    # ── Étape 3 : score pondéré des facteurs ─────────────────────────────────
    factor_list = payload["factor_list"]
    weighted_factor_sum = sum(f["factor_score"] * f["factor_weight"] for f in factor_list)
    max_possible_sum = sum(2 * f["factor_weight"] for f in factor_list)

    result["weighted_factor_sum"] = weighted_factor_sum
    result["max_possible_sum"] = max_possible_sum

    if max_possible_sum == 0:
        adjustment_ratio = 0.0
    else:
        adjustment_ratio = round(weighted_factor_sum / max_possible_sum, 4)

    result["adjustment_ratio"] = adjustment_ratio

    # ── Étape 4 : translation vers probabilité ───────────────────────────────
    adjustment_cap = cfg["estimation"]["adjustment_cap"]
    probability_adjustment = round(adjustment_ratio * adjustment_cap, 4)
    result["probability_adjustment"] = probability_adjustment

    p_raw = base_rate + probability_adjustment

    if blocking_veto:
        # Le veto bloquant écrase tout
        p_prereq = result["p_after_blocking_veto"]
    else:
        p_prereq = round(p_raw * prerequisite_factor, 4)

    result["p_prereq"] = p_prereq

    p_min = cfg["estimation"]["probability_min"]
    p_max = cfg["estimation"]["probability_max"]
    p_estimated = round(min(p_max, max(p_min, p_prereq)), 4)
    result["p_estimated"] = p_estimated

    # ── Étape 5 : fourchette d'incertitude ───────────────────────────────────
    confidence_overall = payload["confidence"]["confidence_overall"]
    width_map = cfg["uncertainty_widths"]
    half_width = level_to_value(width_map, confidence_overall, "uncertainty_widths") / 2

    uncertainty_low = round(min(p_max, max(p_min, p_estimated - half_width)), 4)
    uncertainty_high = round(min(p_max, max(p_min, p_estimated + half_width)), 4)
    uncertainty_width = round(uncertainty_high - uncertainty_low, 4)

    result["uncertainty_low"] = uncertainty_low
    result["uncertainty_high"] = uncertainty_high
    result["uncertainty_width"] = uncertainty_width

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Calcul de l'edge
# ─────────────────────────────────────────────────────────────────────────────

def compute_edge(payload: dict, market_probability: float, p_estimated: float,
                 config: dict, prob_result: dict) -> dict:
    """
    Pipeline d'edge :
    raw_edge → multiplicateurs de qualité → pénalité incertitude → edge ajusté
    """
    result = {}
    cfg = config

    raw_edge = round(p_estimated - market_probability, 4)
    result["raw_edge"] = raw_edge

    # ── Multiplicateurs de qualité ────────────────────────────────────────────
    confidence_overall = payload["confidence"]["confidence_overall"]
    liquidity = payload["screening"]["liquidity_quality"]
    information = payload["screening"]["information_accessibility"]
    event_ambiguity = payload["ambiguity"]["event_ambiguity"]

    cf = level_to_value(cfg["confidence_factors"], confidence_overall, "confidence_factors")
    lf = level_to_value(cfg["liquidity_factors"], liquidity, "liquidity_factors")
    inf_f = level_to_value(cfg["information_factors"], information, "information_factors")
    ea_f = level_to_value(cfg["event_ambiguity_factors"], event_ambiguity, "event_ambiguity_factors")

    result["confidence_factor_applied"] = cf
    result["liquidity_factor_applied"] = lf
    result["information_factor_applied"] = inf_f
    result["event_ambiguity_factor_applied"] = ea_f

    # ── Facteur temps ─────────────────────────────────────────────────────────
    resolution_date_str = payload.get("resolution_date")
    if resolution_date_str:
        try:
            resolution_date = datetime.fromisoformat(resolution_date_str.replace("Z", "+00:00"))
            days_to_resolution = (resolution_date - datetime.now(timezone.utc)).days
        except Exception:
            days_to_resolution = None
    else:
        days_to_resolution = None

    if days_to_resolution is None:
        time_factor = 1.0
        result["days_to_resolution"] = None
    elif days_to_resolution < 30:
        time_factor = cfg["time_factors"]["lt_30"]
        result["days_to_resolution"] = days_to_resolution
    elif days_to_resolution < 120:
        time_factor = cfg["time_factors"]["30_to_120"]
        result["days_to_resolution"] = days_to_resolution
    elif days_to_resolution < 365:
        time_factor = cfg["time_factors"]["120_to_365"]
        result["days_to_resolution"] = days_to_resolution
    else:
        time_factor = cfg["time_factors"]["gt_365"]
        result["days_to_resolution"] = days_to_resolution

    result["time_factor_applied"] = time_factor

    # ── Edge ajusté avant pénalité incertitude ────────────────────────────────
    adjusted_edge = round(raw_edge * cf * lf * inf_f * ea_f * time_factor, 4)
    result["adjusted_edge_before_uncertainty_penalty"] = adjusted_edge

    # ── Pénalité incertitude ──────────────────────────────────────────────────
    uncertainty_width = prob_result["uncertainty_width"]
    unc_threshold = cfg["uncertainty_penalty"]["threshold"]
    unc_factor = cfg["uncertainty_penalty"]["factor"]
    uncertainty_penalty_applied = False

    if uncertainty_width > unc_threshold:
        adjusted_edge = round(adjusted_edge * unc_factor, 4)
        uncertainty_penalty_applied = True

    result["uncertainty_penalty_applied"] = uncertainty_penalty_applied
    result["adjusted_edge"] = adjusted_edge

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Décision
# ─────────────────────────────────────────────────────────────────────────────

def decide_trade(payload: dict, edge_result: dict, config: dict, market_probability: float) -> dict:
    """
    Détermine la décision de trading à partir de l'edge ajusté et des règles de veto.
    """
    result = {}
    cfg = config
    adjusted_edge = edge_result["adjusted_edge"]

    # ── Vetos ─────────────────────────────────────────────────────────────────
    veto_triggered = False
    veto_reasons = []

    resolution_ambiguity = payload["ambiguity"]["resolution_ambiguity"]
    if resolution_ambiguity == "high":
        veto_triggered = True
        veto_reasons.append("resolution_ambiguity = high")

    # Veto sur grand favori avec gap structurel
    # Déclenché APRÈS calcul de l'estimation, quand :
    # - le marché est au-dessus d'un seuil "grand favori" (ex: 75%)
    # - ET le raw_edge est inférieur à -structural_threshold
    # Principe : si le marché price un événement à 80%+ et que le moteur
    # produit une estimation à 55%, l'écart de 25 points n'est pas analytique
    # — c'est un artefact de base_rate sous-estimé ou de facteurs trop négatifs
    # face à un consensus de marché très fort. Le moteur ne peut pas être fiable
    # dans ce cas, quelle que soit la valeur du base_rate assigné.
    extreme_cfg = config.get("extreme_market_veto", {})
    if extreme_cfg.get("enabled", False):
        high_ceiling         = extreme_cfg.get("high_price_ceiling", 0.75)
        low_ceiling          = extreme_cfg.get("low_price_ceiling",  0.25)
        structural_threshold = extreme_cfg.get("structural_edge_threshold", 0.12)
        raw_edge_value       = edge_result.get("raw_edge", 0)

        if market_probability > high_ceiling and raw_edge_value < -structural_threshold:
            veto_triggered = True
            veto_reasons.append(
                f"grand favori avec gap structurel : prix YES {market_probability:.0%} "
                f"> seuil {high_ceiling:.0%} et edge brut {raw_edge_value:+.0%} "
                f"< -{structural_threshold:.0%} — l'ecart est trop grand "
                f"pour être analytique"
            )
        elif market_probability < low_ceiling and raw_edge_value > structural_threshold:
            veto_triggered = True
            veto_reasons.append(
                f"grand outsider avec gap structurel : prix YES {market_probability:.0%} "
                f"< seuil {low_ceiling:.0%} et edge brut {raw_edge_value:+.0%} "
                f"> +{structural_threshold:.0%} — l'ecart est trop grand "
                f"pour être analytique"
            )

    if edge_result.get("adjusted_edge_before_uncertainty_penalty") is not None:
        uncertainty_width = None
        # Récupéré via prob_result transmis dans l'output global
    
    result["veto_triggered"] = veto_triggered
    result["veto_reasons"] = veto_reasons

    # ── Côté de position ──────────────────────────────────────────────────────
    if veto_triggered:
        position_side = "none"
    elif adjusted_edge > 0:
        position_side = "yes"
    elif adjusted_edge < 0:
        position_side = "no"
    else:
        position_side = "none"

    result["position_side"] = position_side

    # ── Décision et sizing ────────────────────────────────────────────────────
    min_edge = cfg["decision_thresholds"]["min_edge"]
    standard_edge = cfg["decision_thresholds"]["standard_edge"]
    bankroll = cfg["sizing"]["paper_bankroll"]
    small_pct = cfg["sizing"]["small_position_pct"]
    std_pct = cfg["sizing"]["standard_position_pct"]

    abs_edge = abs(adjusted_edge)

    if veto_triggered or abs_edge < min_edge:
        decision = "no_trade"
        paper_position_size = 0.0
    elif abs_edge < standard_edge:
        decision = "small_paper_position"
        paper_position_size = round(bankroll * small_pct, 2)
    else:
        decision = "standard_paper_position"
        paper_position_size = round(bankroll * std_pct, 2)

    result["decision"] = decision
    result["paper_position_size"] = paper_position_size

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Assemblage de l'engine_output
# ─────────────────────────────────────────────────────────────────────────────

def run_engine(payload: dict, config: dict, market_probability: float) -> dict:
    """
    Orchestre les 4 étapes : validation → probabilité → edge → décision.
    Retourne un engine_output complet ou lève une ValidationError.
    """
    # 1. Validation
    warnings = validate_payload(payload, config)

    # 2. Probabilité
    prob_result = compute_probability(payload, config)

    # 3. Edge — nécessite resolution_date, on la passe via payload si disponible
    # On injecte la resolution_date depuis le payload si présente
    payload_with_date = dict(payload)
    # resolution_date peut être absent de analysis_payload (il est dans market_request)
    # On essaie de le récupérer ou on le laisse None
    edge_result = compute_edge(
        payload,
        market_probability,
        prob_result["p_estimated"],
        config,
        prob_result
    )

    # 4. Décision
    decision_result = decide_trade(payload, edge_result, config, market_probability)

    # ── Assemblage final ──────────────────────────────────────────────────────
    output = {
        "market_id": payload["market_id"],
        "thesis_id": payload["thesis_id"],
        "analysis_id": payload["analysis_id"],
        "calculation_timestamp": now_iso(),
        "parameters_version": config["parameters_version"],
        "schema_version": config["schema_version"],

        "inputs_summary": {
            "base_rate": prob_result["base_rate"],
            "market_probability_yes": market_probability,
            "confidence_overall": payload["confidence"]["confidence_overall"],
            "liquidity_quality": payload["screening"]["liquidity_quality"],
            "information_accessibility": payload["screening"]["information_accessibility"],
            "event_ambiguity": payload["ambiguity"]["event_ambiguity"],
            "resolution_ambiguity": payload["ambiguity"]["resolution_ambiguity"],
            "factor_count": len(payload["factor_list"]),
        },

        "probability": {
            "weighted_factor_sum": prob_result["weighted_factor_sum"],
            "max_possible_sum": prob_result["max_possible_sum"],
            "adjustment_ratio": prob_result["adjustment_ratio"],
            "probability_adjustment": prob_result["probability_adjustment"],
            "prerequisite_factor": prob_result["prerequisite_factor"],
            "blocking_veto_triggered": prob_result["blocking_veto_triggered"],
            "p_estimated": prob_result["p_estimated"],
            "uncertainty_low": prob_result["uncertainty_low"],
            "uncertainty_high": prob_result["uncertainty_high"],
            "uncertainty_width": prob_result["uncertainty_width"],
        },

        "edge": {
            "raw_edge": edge_result["raw_edge"],
            "confidence_factor_applied": edge_result["confidence_factor_applied"],
            "liquidity_factor_applied": edge_result["liquidity_factor_applied"],
            "information_factor_applied": edge_result["information_factor_applied"],
            "event_ambiguity_factor_applied": edge_result["event_ambiguity_factor_applied"],
            "time_factor_applied": edge_result["time_factor_applied"],
            "days_to_resolution": edge_result["days_to_resolution"],
            "adjusted_edge_before_uncertainty_penalty": edge_result["adjusted_edge_before_uncertainty_penalty"],
            "uncertainty_penalty_applied": edge_result["uncertainty_penalty_applied"],
            "adjusted_edge": edge_result["adjusted_edge"],
        },

        "decision": {
            "veto_triggered": decision_result["veto_triggered"],
            "veto_reasons": decision_result["veto_reasons"],
            "position_side": decision_result["position_side"],
            "decision": decision_result["decision"],
            "paper_position_size": decision_result["paper_position_size"],
        },

        "validation_warnings": [str(w) for w in warnings],
    }

    return output


# ─────────────────────────────────────────────────────────────────────────────
# Interface CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Moteur de calcul paper trading.")
    parser.add_argument("--payload", required=True, help="Chemin vers analysis_payload.json")
    parser.add_argument("--config", default="schemas/system_config.json", help="Chemin vers system_config.json")
    parser.add_argument("--market-prob", type=float, required=False, default=None,
                        help="Probabilité implicite YES du marché (ex: 0.35). "
                             "Si absent, lue depuis market_request.json dans le même dossier.")
    parser.add_argument("--output", default=None, help="Chemin de sortie pour engine_output.json. "
                        "Par défaut : engine_output.json dans le même dossier que le payload.")
    args = parser.parse_args()

    # ── Chemins automatiques ──────────────────────────────────────────────────
    payload_path = Path(args.payload)
    market_dir = payload_path.parent

    # Output par défaut dans le même dossier que le payload
    output_path = args.output if args.output else str(market_dir / "engine_output.json")

    # Config par défaut
    config_path = args.config

    # Chargement payload
    try:
        payload = load_json(str(payload_path))
    except FileNotFoundError:
        print(f"[ERREUR] Fichier payload introuvable : {payload_path}", file=sys.stderr)
        sys.exit(1)

    # Chargement config
    try:
        config = load_json(config_path)
    except FileNotFoundError:
        print(f"[ERREUR] Fichier config introuvable : {config_path}", file=sys.stderr)
        sys.exit(1)

    # ── Résolution du prix marché ─────────────────────────────────────────────
    market_prob = args.market_prob

    if market_prob is None:
        # Cherche market_request.json dans le même dossier que le payload
        mr_path = market_dir / "market_request.json"
        if mr_path.exists():
            try:
                mr = load_json(str(mr_path))
                market_prob = mr.get("market_probability_yes")
                if market_prob is not None:
                    print(f"  Prix lu depuis market_request.json : {market_prob:.0%}")
                else:
                    print(f"[ERREUR] market_request.json trouvé mais 'market_probability_yes' absent.",
                          file=sys.stderr)
                    sys.exit(1)
            except Exception as e:
                print(f"[ERREUR] Impossible de lire market_request.json : {e}", file=sys.stderr)
                sys.exit(1)
        else:
            print(
                f"[ERREUR] --market-prob non fourni et aucun market_request.json trouvé dans {market_dir}.\n"
                f"  Soit lance fetch_market.py d'abord, soit passe --market-prob manuellement.",
                file=sys.stderr
            )
            sys.exit(1)

    # Validation du prix
    if not (0.01 <= market_prob <= 0.99):
        print(f"[ERREUR] market_prob doit être entre 0.01 et 0.99. Reçu : {market_prob}", file=sys.stderr)
        sys.exit(1)

    # Exécution
    try:
        output = run_engine(payload, config, market_prob)
    except ValidationError as e:
        print(f"\n[REFUS MOTEUR] Payload invalide :\n  → {e}\n", file=sys.stderr)
        sys.exit(2)

    # Affichage résumé
    print("\n" + "═" * 60)
    print("  MOTEUR — RÉSULTAT")
    print("═" * 60)
    print(f"  Marché           : {output['market_id']}")
    print(f"  p_estimée        : {output['probability']['p_estimated']:.1%}  "
          f"[{output['probability']['uncertainty_low']:.1%} – {output['probability']['uncertainty_high']:.1%}]")
    print(f"  Prix marché      : {market_prob:.1%}")
    print(f"  Edge brut        : {output['edge']['raw_edge']:+.1%}")
    print(f"  Edge ajusté      : {output['edge']['adjusted_edge']:+.1%}")
    print(f"  Côté             : {output['decision']['position_side'].upper()}")
    print(f"  Décision         : {output['decision']['decision'].upper()}")
    if output['decision']['paper_position_size'] > 0:
        print(f"  Taille paper     : {output['decision']['paper_position_size']} €")
    if output['decision']['veto_triggered']:
        print(f"  ⚠ VETO          : {', '.join(output['decision']['veto_reasons'])}")
    if output['validation_warnings']:
        print(f"\n  Avertissements :")
        for w in output['validation_warnings']:
            print(f"    {w}")
    print("═" * 60 + "\n")

    # Sauvegarde
    save_json(output, output_path)
    print(f"  Output sauvegardé : {output_path}\n")


if __name__ == "__main__":
    main()