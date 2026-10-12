import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
from core.history import HistoryStore
from core.history_search import rank
from core.reporting import SessionReport
from core.watcher import ErrorEvent
from test_history import TinyEmbedding


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = HistoryStore(self.tmp.name, TinyEmbedding())
        self.keys = {}
        for error in ['ERROR: Connection to localhost:8080 timed out', 'ERROR: Permission denied /tmp/a',
                      'kernel: Out of memory: Kill process 42 (python3)', 'ERROR: screen is not installed']:
            self.keys[error] = self.store.record_occurrence(ErrorEvent('/tmp/log',error,'now'))[0]

    def test_recognized_queries_reject_wrong_symptoms_even_with_identical_vectors(self):
        for query, text in [('connection timeout','timed out'), ('Arbeitsspeicher','Out of memory'),
                            ('недостаточно памяти','Out of memory'), ('program missing','screen')]:
            rows = self.store.search_problems(query,10)
            self.assertEqual(len(rows),1,query)
            self.assertIn(text,rows[0]['error'])
            self.assertIn('match_reason',rows[0])

    def test_eligibility_filters_run_before_result_limit(self):
        key = self.keys['ERROR: screen is not installed']
        rows = self.store.search_problems('program missing',1,eligible_ids=[key])
        self.assertEqual([item['id'] for item in rows],[key])
        self.assertEqual(self.store.search_problems('program missing',1,eligible_ids=[]),[])

    def test_solution_text_is_indexed_and_edits_replace_it(self):
        key = self.keys['ERROR: screen is not installed']
        sid = self.store.save_solution(key, [], 'Restore orbitalwidget configuration', 'user')
        self.assertEqual(self.store.search_problems('orbitalwidget',1)[0]['id'],key)
        self.store.edit_solution(sid, [], 'Restore another configuration')
        self.store.search_problems('orbitalwidget',10)
        text = self.store.search_index.get(ids=['problem:'+key])['documents'][0]
        self.assertNotIn('orbitalwidget',text)
        self.assertIn('another configuration',text)

    def test_empty_database_unknown_relevance_and_empty_query(self):
        self.assertEqual(self.store.search_problems('',10),[])
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(HistoryStore(root,TinyEmbedding()).search_problems('memory'),[])
        index = Mock()
        candidate={'id':'p','error':'ERROR strange event','search_text':'ERROR strange event'}
        index.get.return_value={'ids':[],'documents':[]}
        index.query.return_value={'ids':[['problem:p']], 'distances':[[1.9]]}
        self.assertEqual(rank(index,[candidate],'purple dancing unicorn','problem'),[])

    def test_linux_reference_search_is_separate_from_incident_statistics(self):
        rows = self.store.search_linux_references('Arbeitsspeicher')
        self.assertEqual({row['category'] for row in rows},{'default-linux-memory'})
        self.assertEqual(self.store.errors.count(),4)
        self.assertEqual(self.store.occurrences.count(),4)


class RealCpuSearchTests(unittest.TestCase):
    def test_cpu_embeddings_paraphrase_without_literal_match(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/'.chroma_models').symlink_to(config.BASE_DIR/'.chroma_models',target_is_directory=True)
            store=HistoryStore(str(Path(root)/'db'))
            for error in ['ERROR: screen is not installed', 'ERROR: Permission denied /tmp/test', 'kernel: Out of memory: Kill process 42']:
                store.record_occurrence(ErrorEvent('/tmp/log',error,'now'))
            result=store.search_problems('program missing',3)
            self.assertEqual(len(result),1)
            self.assertIn('screen',result[0]['error'])
            self.assertEqual(store.search_problems('purple dancing unicorn'),[])


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=HistoryStore(self.tmp.name,TinyEmbedding())
        self.event=ErrorEvent('/tmp/log','ERROR: Permission denied /tmp/a','now')
        self.key,self.first=self.store.record_occurrence(self.event)
        _,self.second=self.store.record_occurrence(self.event)
        self.sid=self.store.save_solution(self.key,[], 'Manual repair','user')
        self.cases=[]
        for occurrence,outcome in [(self.first,'yes'),(self.second,'no')]:
            case=SessionReport(self.event,self.store.context)
            case.data.update(error_key=self.key,occurrence_ids=[occurrence],outcome=outcome)
            case.finish()
            self.store.save_investigation(case.data)
            self.store.record_attempt(self.key,occurrence,self.sid,outcome,case_id=case.data['id'],attempt_id='attempt-'+case.data['id'])
            self.cases.append(case.data)
        self.scenario=self.store.save_playbook(self.event.line, {'hypothesis':'Inspect access','commands':[{'command':'stat -- /tmp/a'}]}, {}, case_id=self.cases[0]['id'])
        self.store.save_playbook(self.event.line, {'hypothesis':'Inspect access','commands':[{'command':'stat -- /tmp/a'}]}, {}, case_id=self.cases[1]['id'])

    def test_delete_case_recalculates_ratings_and_keeps_shared_knowledge(self):
        remaining=copy.deepcopy(self.cases[1])
        remaining['recurrence_of_case']=self.cases[0]['case_number']
        self.store.save_investigation(remaining)
        preview=self.store.deletion_preview(case_id=self.cases[0]['id'])
        self.assertEqual(preview['counts']['Solutions'],0)
        self.assertEqual(preview['counts']['Occurrences'],1)
        self.store.delete_case(self.cases[0]['id'])
        summary=self.store.summary(self.key)
        self.assertEqual((summary['seen'],summary['solved'],summary['failed'],summary['attempts']),(1,0,1,1))
        self.assertEqual((summary['solutions'][0]['helped'],summary['solutions'][0]['failed']),(0,1))
        self.assertEqual(self.store.solutions.count(),1)
        self.assertNotIn('recurrence_of_case',self.store.case_records()[0])
        self.assertEqual(self.store.list_playbooks()[0]['case_ids'],[self.cases[1]['id']])

    def test_delete_problem_cascades_without_touching_other_errors_or_linux_library(self):
        other_key,other_occ=self.store.record_occurrence(ErrorEvent('/tmp/log','ERROR: Connection refused localhost:80','now'))
        other_sid=self.store.save_solution(other_key,[],'Other fix','user')
        self.store.search_problems('permissions')
        counts=self.store.delete_problem(self.key)
        self.assertEqual(counts['Cases'],2)
        self.assertEqual(self.store.errors.get()['ids'],[other_key])
        self.assertEqual(self.store.occurrences.get()['ids'],[other_occ])
        self.assertEqual(self.store.solutions.get()['ids'],[other_sid])
        self.assertEqual((self.store.attempts.count(),self.store.investigations.count(),self.store.playbooks.count()),(0,0,0))
        self.assertEqual(self.store.search_index.get(ids=['problem:'+self.key])['ids'],[])
        self.assertEqual(self.store.linux_library.count(),25)

    def test_shared_occurrence_survives_and_independent_user_scenario_is_preserved(self):
        case=copy.deepcopy(self.cases[1])
        case['occurrence_ids'].append(self.first)
        self.store.save_investigation(case)
        user_id=self.store.save_playbook(self.event.line,{'hypothesis':'User policy','commands':[{'command':'id'}]}, {},source='user',case_id=self.cases[0]['id'])
        self.store.delete_case(self.cases[0]['id'])
        self.assertEqual(self.store.occurrences.count(),2)
        user=next(row for row in self.store.list_playbooks() if row['id']==user_id)
        self.assertEqual(user['case_ids'],[])
        self.assertEqual(user['source'],'user')

    def test_active_case_blocks_problem_and_case_deletion(self):
        case=copy.deepcopy(self.cases[0]);case['status']='in_progress'
        self.store.save_investigation(case)
        self.assertTrue(self.store.deletion_preview(problem_id=self.key)['blocked'])
        for operation,id_ in [(self.store.delete_problem,self.key),(self.store.delete_case,case['id'])]:
            with self.assertRaisesRegex(ValueError,'active investigation'):
                operation(id_)
        self.assertEqual((self.store.errors.count(),self.store.investigations.count(),self.store.attempts.count()),(1,2,2))

    def test_missing_target_is_rejected_without_deleting_anything(self):
        for operation in [self.store.delete_case,self.store.delete_problem]:
            with self.assertRaises(ValueError):
                operation('does-not-exist')
        self.assertEqual(self.store.errors.count(),1)
