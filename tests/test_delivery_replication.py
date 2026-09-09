import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from delivery import sha
from delivery_replication import release_evidence


class ReplicationDeliveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root/'evidence').mkdir()
        self.validator,self.reader = Mock(),Mock()

    def fixture(self, status='complete'):
        freeze = self.root/'evidence/audit_replication_final_freeze.json'
        freeze.write_text('{}')
        directory = self.root/'evidence/final-fixture'
        registration = {'status':status,'final_freeze_sha256':sha(freeze),'directory':str(directory)}
        (self.root/'evidence/audit_replication_test_registration.json').write_text(json.dumps(registration))
        self.reader.return_value = {'directory':str(directory),'scheduled_per_arm':160,'sources_sha256':{},
            'arms':{arm:{'rows':[{'case_id':str(i)} for i in range(160)],'business_summary':{'cases':160},
                'counts':{'business':160,'facts':158,'communication':159,'joint':157},'critical_issues':[]}
                    for arm in ('identity_multi','structured_multi')}}

    def test_absent_final_evidence_refuses_activation_without_runtime_or_model_work(self):
        with self.assertRaises(RuntimeError):
            release_evidence(self.root,final_validator=self.validator,phase_reader=self.reader)
        self.validator.assert_not_called()
        self.reader.assert_not_called()

    def test_running_experiment_cannot_activate(self):
        self.fixture(status='running')
        with self.assertRaises(RuntimeError):
            release_evidence(self.root,final_validator=self.validator,phase_reader=self.reader)
        self.reader.assert_not_called()

    def test_final_failures_stay_visible_after_validated_complete_measurement(self):
        self.fixture()
        evidence = release_evidence(self.root,final_validator=self.validator,phase_reader=self.reader)
        self.assertEqual(evidence['report_audits']['structured_multi']['facts'],158)
        self.assertEqual(evidence['report_audits']['structured_multi']['scheduled'],160)
        self.assertFalse(evidence['original_v2_gate_passed'])
        self.validator.assert_called_once()
        self.reader.assert_called_once_with('test')

    def test_partial_comparator_cannot_be_omitted_to_activate(self):
        self.fixture()
        self.reader.return_value['arms']['identity_multi']['rows'].pop()
        with self.assertRaises(ValueError):
            release_evidence(self.root,final_validator=self.validator,phase_reader=self.reader)


if __name__=='__main__':
    unittest.main()
