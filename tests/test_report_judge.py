import json
import pytest

from evaluation.report_judge import judge, segment_report
from research.model_client import ModelCallError


class FixtureClient:
    def __init__(self, *claims):
        self.claims = claims

    def chat(self,*args,**kwargs):
        return {'finish_reason':'tool_calls','message':{'tool_calls':[{'function':{'name':'submit_audit',
            'arguments':json.dumps({'claims':self.claims})}}]},'estimated_cost_cny':'0'}


def test_invented_evidence_or_segments_cannot_produce_a_pass():
    for claim in [
        {'segment_id':'S001','status':'supported','evidence_ids':['MISSING'],'explanation':'fake'},
        {'segment_id':'S002','status':'supported','evidence_ids':['TOOL_1'],'explanation':'fake'},
        {'segment_id':'S001','status':'supported','evidence_ids':[],'explanation':'no source'}]:
        with pytest.raises(ValueError):
            judge('Cost is $2.',{'TOOL_1':{'price':8}},client=FixtureClient(claim))


def test_claim_conflict_and_missing_evidence_are_separate_nonpasses():
    for status,expected in [('contradicted','unsupported'),('unverifiable','insufficient_evidence')]:
        claim = {'segment_id':'S001','status':status,'evidence_ids':['TOOL_1'],'explanation':'fixture'}
        result = judge('Cotton shirt',{'TOOL_1':{}},client=FixtureClient(claim))
        assert result['decision']['verdict']==expected
        assert result['decision']['claims'][0]['quote']=='Cotton shirt'


def test_skipped_tail_or_duplicate_segments_cannot_pass():
    claim={'segment_id':'S001','status':'supported','evidence_ids':['TOOL_1'],'explanation':'fixture'}
    for claims in [(claim,), (claim,claim)]:
        with pytest.raises(ValueError,match='every segment'):
            judge('Price is $8.50. It is paid.',{'TOOL_1':{}},client=FixtureClient(*claims))


def test_segment_boundaries_preserve_prices_and_cover_all_text():
    answer='  Price is $8.50.\n一件商品；包装未知。Tail without punctuation '+('x'*1100)+' end'
    segments=segment_report(answer)
    assert segments[0]['text']=='Price is $8.50.'
    assert all(answer[s['start']:s['end']]==s['text'] and len(s['text'])<=1000 for s in segments)
    assert ''.join(c for s in segments for c in s['text'] if not c.isspace())==''.join(c for c in answer if not c.isspace())


def test_transient_transport_retry_is_bounded_and_keeps_the_failure():
    class TransientClient(FixtureClient):
        calls=0
        def chat(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1: raise ModelCallError('timeout',retryable=True)
            return super().chat(*args,**kwargs)
    claim={'segment_id':'S001','status':'unverifiable','evidence_ids':['TOOL_1'],'explanation':'not established'}
    client=TransientClient(claim)
    result=judge('Cotton shirt',{'TOOL_1':{}},client=client)
    assert client.calls==2 and result['decision']['verdict']=='insufficient_evidence'
    assert result['response_metadata']['transport_attempts']==2
    assert len(result['response_metadata']['transient_errors'])==1


def test_invalid_audit_is_not_resampled_for_a_lucky_pass():
    class CountingClient(FixtureClient):
        calls=0
        def chat(self,*args,**kwargs):
            self.calls+=1
            return super().chat(*args,**kwargs)
    client=CountingClient({'segment_id':'S999','status':'supported','evidence_ids':['TOOL_1'],'explanation':'bad'})
    with pytest.raises(ValueError): judge('Cotton shirt',{'TOOL_1':{}},client=client)
    assert client.calls==1
