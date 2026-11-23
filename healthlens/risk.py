"""Risk level, risk score and risk factors for each health category.

Built on top of the category checks in healthlens.scenarios (the same checks the 900-patient validation covers):
  * risk factors = the checks that found something noteworthy in the patient's own records,
  * risk score   = how many of the category's key checks are risk factors ("2 of 6"),
  * risk level   = Low / Moderate / High, from the same rule the category status uses,
  * completeness = how many key measures the uploaded documents contain, and which ones are missing.

It works with whatever has been uploaded, even a single document: the level is then based on the available
measures, and the missing ones are named so the patient knows the result may be incomplete.
These are transparent demonstration rules for education, not a clinical risk calculator and not a diagnosis.
"""
from __future__ import annotations

LEVEL_OF_STATUS = {'pattern': 'High', 'partial': 'Moderate', 'none': 'Low'}


def _level_from_count(factors: int) -> str:
    """The category rule (healthlens.scenarios.summarise): 3+ related signals = pattern, 2 = partial."""
    return 'High' if factors >= 3 else 'Moderate' if factors == 2 else 'Low'
DISCLAIMER = ('Educational risk score, based only on the documents you uploaded. '
              'It is not a diagnosis; talk to a healthcare professional about your results.')


def _names(items):
    items = list(items)
    if len(items) <= 1:
        return ''.join(items)
    return ', '.join(items[:-1]) + ' and ' + items[-1]


# Women's health for a female patient without uploaded cycle data: the rule checks need cycles, but the trained
# model can still score from the other documents (labs, vitals), so cycle data is listed as missing.
PCOS_INPUTS = {'Cycle regularity': 'cycles', 'Weight / BMI': 'vitals', 'Glycemic markers': 'labs',
               'Lipids (TG / HDL)': 'labs', 'Cycle-related symptoms': 'cycles'}


def _pcos_without_cycles(model):
    used = set((model or {}).get('sources_used') or [])
    have = [n for n, src in PCOS_INPUTS.items() if src in used]
    missing = [n for n in PCOS_INPUTS if n not in have]
    total = len(PCOS_INPUTS)
    out = {'applicable': True, 'level': None, 'label': 'Not enough data', 'risk_score': None, 'source': 'rules',
           'protective': [], 'rule_factors': [], 'score': 0, 'out_of': total, 'available': len(have),
           'completeness': round(100 * len(have) / total), 'limited': True, 'factors': [], 'missing': missing,
           'absent': missing, 'outside': [], 'summary': 'No key measures uploaded yet.',
           'missing_note': f'Missing: {_names(missing)}.', 'disclaimer': DISCLAIMER}
    if model and have:
        out.update(level=model['level'], label=f'{model["level"]} risk', risk_score=model['score'], source='trained model',
                   factors=model['raising'] if model['level'] != 'Low' else [], protective=model['lowering'],
                   summary=f'{len(have)} of {total} key inputs available.',
                   missing_note=f'{len(missing)} of {total} key inputs missing: {_names(missing)}. This prediction is based on '
                                f'the other {len(have)} ({_names(have)}), so the prediction score may not be accurate.')
    return out


def assess(scenario: dict | None, missing_in_period: set | None = None, model: dict | None = None) -> dict | None:
    """Risk summary for one category (one item of analysis['scenarios']).

    missing_in_period: names of measures that are missing in the selected span but exist in the full record
    (so the message can point to 'Full record' instead of asking for more documents).
    model: this category's result from the trained model (healthlens.risk_model.score). When given, the level and
    the risk score come from the model and the risk factors are the measures that raised the score most; the rule
    checks are still shown as the detailed explanation. Without it, the rule-based level is used."""
    if not scenario:
        return None
    if scenario.get('key') == 'pcos' and scenario['status'] == 'not_applicable' and (scenario.get('note') or '').startswith('No menstrual-cycle data'):
        return _pcos_without_cycles(model)
    if scenario['status'] == 'not_applicable':
        return {'applicable': False, 'level': None, 'label': 'Not applicable', 'note': scenario.get('note', ''),
                'factors': [], 'missing': [], 'score': 0, 'out_of': 0}
    checks = scenario['checks']
    total = len(checks)
    factors = [{'name': c['signal'], 'detail': c['text']} for c in checks if c['status'] == 'signal']
    missing = [c['signal'] for c in checks if c['status'] == 'missing']
    available = total - len(missing)
    status = scenario['status']
    if available == 0:
        level = None
    elif status in LEVEL_OF_STATUS:
        level = LEVEL_OF_STATUS[status]
    else:                                    # 'insufficient': one measure available -> same rule, marked limited
        level = _level_from_count(len(factors))
    limited = available < (total + 1) // 2 or status == 'insufficient'
    later = [m for m in missing if missing_in_period and m in missing_in_period]
    absent = [m for m in missing if m not in later]
    if level is None:
        label = 'Not enough data'
        summary = 'No key measures in this period.' if later and not absent else 'No key measures uploaded yet.'
    else:
        label = f'{level} risk'
        summary = f'{len(factors)} of {total} risk factors found.'
    risk_score, protective, source, rule_factors = None, [], 'rules', factors
    gaps = missing                           # rules look at this period only
    if model and (level is not None or model.get('sources_used')):
        level, risk_score, source = model['level'], model['score'], 'trained model'
        label = f'{level} risk'
        # For a Low score no measure noticeably raises the risk, so none is called a risk factor.
        factors = model['raising'] if level != 'Low' else []
        protective = model['lowering']
        gaps = absent                        # the trained score uses all uploaded documents
        summary = f'{total - len(gaps)} of {total} key inputs available.'
    notes = []
    if gaps and level is not None:
        used = [c['signal'] for c in checks if c['signal'] not in gaps]
        what = 'prediction score' if risk_score is not None else 'result'
        notes.append(f'{len(gaps)} of {total} key inputs missing: {_names(gaps)}. '
                     f'This prediction is based on the other {len(used)} ({_names(used)}), '
                     f'so the {what} may not be accurate.')
    elif absent:
        notes.append(f'Missing: {_names(absent)}.')
    if later and risk_score is None:         # the trained score already uses every document
        notes.append(f'Not in this period: {_names(later)} (see Full record).')
    return {'applicable': True, 'level': level, 'label': label, 'risk_score': risk_score, 'source': source,
            'protective': protective, 'rule_factors': rule_factors, 'score': len(rule_factors), 'out_of': total,
            'available': available, 'completeness': round(100 * available / total) if total else 0,
            'limited': limited, 'factors': factors, 'missing': missing, 'absent': absent, 'outside': later,
            'summary': summary,
            'missing_note': ' '.join(notes), 'disclaimer': DISCLAIMER}
