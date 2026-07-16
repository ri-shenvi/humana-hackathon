"""Print the impact number for the deck.

    python -m caregap_compass.scripts.compute_impact
    python -m caregap_compass.scripts.compute_impact CDC-H
"""

from __future__ import annotations

import logging
import sys

from .. import bq, impact


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.ERROR)
    measure_id = argv[1] if len(argv) > 1 else "CBP"

    result = impact.headline(measure_id)
    if result["status"] != "ok":
        print(f"error: {result.get('error_message')}", file=sys.stderr)
        return 1

    print()
    print("=" * 78)
    print("  THE SLIDE SENTENCE")
    print("=" * 78)
    print()
    for line in _wrap(result["sentence"], 76):
        print(f"  {line}")
    print()

    print("-" * 78)
    print(f"  {result['measure_id']} - {result['measure_name']}")
    print("-" * 78)
    print(f"  weight                {result['weight']:g}x ({result['plain_weight']})")
    print(f"  scored star measure   {result['is_scored_star_measure']}")
    print(
        f"  compliant / eligible  {result['members_compliant']} / "
        f"{result['members_eligible']}  =  {result['current_rate_pct']}%"
    )
    print(f"  current star rating   {result['current_star_rating']}")
    print(f"  open gaps             {result['open_gap_count']}")
    print(f"  at risk / trending    {result['at_risk']} / {result['trending']}")
    print(f"  benchmarks            {result['benchmarks']}")
    print()

    projection = result.get("projection")
    if projection:
        print("  Grounded projection (not an assumption)")
        print(
            f"    best {result['measure_id']} intervention on record   "
            f"{projection['intervention_type']} ({projection['intervention_year']})"
        )
        print(
            f"    closes                                {projection['closure_rate_pct']}% "
            f"via {projection['primary_channel']}"
        )
        print(f"    expected closes from {result['open_gap_count']} open gaps        "
              f"{projection['expected_closes']}")
        if projection.get("cost_per_closure_usd"):
            print(f"    cost per closure                      "
                  f"${projection['cost_per_closure_usd']:,.0f}")
        print()

    target = result.get("target") or {}
    ceiling = result.get("ceiling") or {}
    print("  Closing gaps -> star rating")
    print(f"  {'closes':>8} {'% of open':>10} {'rate':>8} {'stars':>7}   note")
    print(f"  {'0':>8} {'0%':>10} {str(result['current_rate_pct']) + '%':>8} "
          f"{result['current_star_rating']:>7}   today")
    for rung in result["star_ladder"]:
        note = ""
        if rung == target:
            note = "<- projected at historical closure rate"
        elif rung == ceiling:
            note = "ceiling (every open gap closed)"
        print(
            f"  {rung['closes_needed']:>8} {str(rung['pct_of_open_gaps']) + '%':>10} "
            f"{str(rung['resulting_rate_pct']) + '%':>8} {rung['resulting_stars']:>7}"
            f"   {note}"
        )
    print()

    print("-" * 78)
    print("  PLAN-WIDE OPPORTUNITY (weight x reachable star gain)")
    print("-" * 78)
    print(
        f"  {'measure':<8} {'w':>4} {'open':>5} {'rate':>7} {'star':>5} "
        f"{'gain':>5} {'next':>5} {'score':>6}"
    )
    for row in impact.rank_measures_by_opportunity():
        print(
            f"  {row['measure_id']:<8} {row['weight']:>4g} {row['open_gap_count']:>5} "
            f"{row['current_rate_pct']:>6.1f}% {row['current_star_rating']:>5} "
            f"{row['reachable_star_gain']:>5} "
            f"{str(row['closes_for_next_star'] or '-'):>5} "
            f"{row['weighted_opportunity']:>6.1f}"
        )
    print()

    segments = impact.segment_breakdown(measure_id)
    print("-" * 78)
    print(f"  WEAKEST {measure_id} SEGMENTS (n >= {segments['min_eligible']} only)")
    print("-" * 78)
    for row in segments["reportable"][:5]:
        print(
            f"  {row.get('plan_type', '?'):<6} {row.get('age_band', '?'):<8} "
            f"{row.get('gender', '?'):<3} "
            f"{row.get('members_compliant')}/{row.get('members_eligible'):<5} "
            f"{row.get('rate_pct')}%"
        )
    if not segments["reportable"]:
        print("  (none large enough to report)")
    print()
    print(f"  {segments['note']}")
    print()

    print(f"  source: {result['source']}")
    print()
    return 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
