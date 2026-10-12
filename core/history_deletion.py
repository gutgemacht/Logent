"""Explicit cascade deletion. Shared knowledge and other cases are retained."""
import json


def rows(collection, **kwargs):
    result = collection.get(include=['documents'], **kwargs)
    return [dict(json.loads(doc), id=id_) for id_, doc in zip(result['ids'], result['documents'])]


def occurrence_ids(record):
    return set(record.get('occurrence_ids') or ([record['occurrence_id']] if record.get('occurrence_id') else []))


def preview(store, *, problem_id=None, case_id=None):
    if bool(problem_id) == bool(case_id):
        raise ValueError('Select exactly one problem or case')
    all_cases = store.case_records()
    if problem_id:
        problem = store.errors.get(ids=[problem_id], include=['documents', 'metadatas'])
        if not problem['ids']:
            raise ValueError('Problem not found')
        selected = [case for case in all_cases if case.get('error_key') == problem_id]
        key = problem_id
        title = problem['documents'][0]
    else:
        selected = [case for case in all_cases if case['id'] == case_id]
        if not selected:
            raise ValueError('Investigation case not found')
        key = selected[0].get('error_key')
        title = selected[0]['case_number'] + ' · ' + selected[0]['error']
    case_ids = {case['id'] for case in selected}
    attempts = rows(store.attempts, **({'where':{'error_key':key}} if key else {}))
    deleted_attempts = attempts if problem_id else [item for item in attempts if item.get('case_id') in case_ids or item['id'] == selected[0].get('attempt_id')]
    if problem_id:
        removed_occurrences = store.occurrences.get(where={'error_key':key}, include=[])['ids']
        removed_solutions = store.solutions.get(where={'error_key':key}, include=[])['ids']
    else:
        candidates = set().union(*(occurrence_ids(item) for item in selected + deleted_attempts))
        retained = set().union(*(occurrence_ids(item) for item in all_cases if item['id'] not in case_ids))
        retained |= set().union(*(occurrence_ids(item) for item in attempts if item not in deleted_attempts))
        candidates -= retained
        stored = rows(store.occurrences, ids=sorted(candidates)) if candidates else []
        removed_occurrences = [item['id'] for item in stored if item['error_key'] == key]
        removed_solutions = []  # Solutions can be shared with other occurrences and cases.
    removed_scenarios, updates = [], []
    for scenario in store.list_playbooks():
        linked = set(scenario.get('case_ids', []))
        if problem_id and scenario.get('source') == 'agent':
            from core.history import normalize_error
            metadata = problem['metadatas'][0] or {}
            environment = scenario.get('environment', {})
            same_environment = all(metadata.get(field) and metadata[field] == environment.get(field)
                                   for field in ('machine_id', 'os', 'user'))
            if (same_environment and normalize_error(scenario['symptom']) == normalize_error(title)
                    and not (linked - case_ids)):
                removed_scenarios.append(scenario['id'])
                continue
        if not linked & case_ids:
            continue
        remaining = sorted(linked - case_ids)
        if not remaining and scenario.get('source') == 'agent':
            removed_scenarios.append(scenario['id'])
        else:
            updates.append(dict(scenario, case_ids=remaining))
    numbers = {case['case_number'] for case in selected}
    recurrence_updates = [dict(case) for case in all_cases if case['id'] not in case_ids and case.get('recurrence_of_case') in numbers]
    for case in recurrence_updates:
        case.pop('recurrence_of_case', None)
    return dict(title=title, error_key=key, problem_ids=[problem_id] if problem_id else [],
        case_ids=sorted(case_ids), occurrence_ids=removed_occurrences,
        solution_ids=removed_solutions, attempt_ids=[item['id'] for item in deleted_attempts],
        scenario_ids=removed_scenarios, scenario_updates=updates, recurrence_updates=recurrence_updates,
        blocked=any(case.get('status') == 'in_progress' for case in selected),
        counts={'Problems':1 if problem_id else 0, 'Cases':len(case_ids), 'Occurrences':len(removed_occurrences),
                'Solutions':len(removed_solutions), 'Attempts / ratings':len(deleted_attempts), 'Derived diagnostic scenarios':len(removed_scenarios)})


def delete(store, *, problem_id=None, case_id=None):
    plan = preview(store, problem_id=problem_id, case_id=case_id)
    if plan['blocked']:
        raise ValueError('Finish or cancel the active investigation before deleting its data')
    # Recompute at submission time; preview counts are never trusted input.
    for field, collection in [('attempt_ids', store.attempts), ('case_ids', store.investigations),
                              ('occurrence_ids', store.occurrences), ('solution_ids', store.solutions),
                              ('scenario_ids', store.playbooks), ('problem_ids', store.errors)]:
        if plan[field]:
            collection.delete(ids=plan[field])
    for scenario in plan['scenario_updates']:
        id_ = scenario.pop('id')
        store.playbooks.update(ids=[id_], documents=[scenario['symptom'] + '\n' + json.dumps(scenario)],
            embeddings=store.embedding_function([scenario['symptom'] + '\n' + scenario['title'] + '\n' + scenario['category']]))
    for case in plan['recurrence_updates']:
        store.save_investigation(case)
    index_ids = ['problem:' + id_ for id_ in plan['problem_ids']]
    # A remaining problem must be reindexed after its rating/case contents change.
    if case_id:
        key = plan['error_key']
        if key:
            index_ids.append('problem:' + key)
    if index_ids:
        store.search_index.delete(ids=index_ids)
    return plan['counts']
