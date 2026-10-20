import copy
import io
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'multica-skills/slack-ticket-actions/scripts'
sys.path.insert(0, str(SCRIPTS))
import ticket_actions as t
from codex_context import search, Unavailable, Codex

EVENT = dict(teamId='T1', channelId='C1', senderUserId='U1', messageTs='100.000001', threadTs='99.000001', text='<@U1> reply and close ENG-123')
PLAN = dict(issue='ENG-123', requestConfirmed=True, requestText=EVENT['text'], actions=['reply', 'close'],
    rootCause=dict(confirmed=True, summary='Duplicate configuration.', evidence=[dict(source='slack', messageTs='99.000001', quote='Duplicate configuration caused the error.')]),
    comment='#intercom\nThe error was caused by duplicate configuration.', fields={'resolution': {'id': '1'}})

class FakeApi:
    owner='U1'
    team='T1'
    base='https://jira.test'
    jira_account='peter'
    def __init__(self):
        self.messages=[dict(ts='99.000001', user='U2', text='ENG-123: Duplicate configuration caused the error.'),
                       dict(ts=EVENT['messageTs'], user='U1', text=EVENT['text'])]
        self.issue=dict(id='123', key='ENG-123', fields=dict(status=dict(id='1', name='In Progress'), resolution=None))
        self.transition=dict(id='9', to=dict(id='2',name='Resolved'), fields={'resolution':dict(required=True, allowedValues=[dict(id='1',name='Fixed')])})
        self.comment_rows=[]
        self.writes=[]
        self.slack_writes=[]
        self.fail_post=None
        self.after_post_error=False
    def slack(self, method, body):
        if method=='auth.test': return dict(ok=True,user_id='U1', team_id='T1')
        if method=='conversations.replies': return dict(ok=True, messages=copy.deepcopy(self.messages), response_metadata={})
        self.slack_writes.append((method,body))
        if method=='chat.postMessage':self.messages.append(dict(ts='200.000001',user='U1',text=body['text']))
        if method=='chat.update':
            for message in self.messages:
                if message['ts']==body['ts']:message['text']=body['text']
        return dict(ok=True,ts='200.000001')
    def jira(self,path,method='GET',body=None):
        if method=='POST':
            if self.fail_post: raise t.Blocked(self.fail_post)
            self.writes.append((path,body))
            if path.endswith('/comment'):
                comment=dict(id=str(len(self.comment_rows)+1), body=body['body'],author=dict(accountId='peter'))
                self.comment_rows.append(comment)
                if self.after_post_error: raise t.Blocked('network_or_response_unknown')
                return comment
            if path.endswith('/transitions'):
                self.issue['fields'].update(body['fields'])
                self.issue['fields']['status']=dict(self.transition['to'])
                if self.after_post_error: raise t.Blocked('network_or_response_unknown')
                return {}
        if path.startswith('issue/') and '/comment/' in path: return self.comment_rows[int(path.split('/')[-1])-1]
        if '/comment?' in path: return dict(comments=self.comment_rows,total=len(self.comment_rows))
        if '/transitions?' in path: return dict(transitions=[self.transition])
        if path=='myself': return dict(accountId='peter')
        return copy.deepcopy(self.issue)

class TicketActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.journal=t.Journal(self.temp.name)
        self.api=FakeApi()
        self.plan=copy.deepcopy(PLAN)
    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()
    def execute(self): return t.run(self.api,EVENT,self.plan,self.journal,True)
    def test_dry_run_has_no_external_writes(self):
        result=t.run(self.api,EVENT,self.plan,self.journal)
        self.assertTrue(result['dry_run'])
        self.assertFalse(self.api.writes or self.api.slack_writes)
    def test_success_and_duplicate_have_one_comment_and_transition(self):
        result=self.execute()
        self.assertEqual(result['completed'],['reply','close'])
        self.execute()
        self.assertEqual(len(self.api.writes),2)
        self.assertEqual([b['name'] for m,b in self.api.slack_writes if m=='reactions.add'][:2],['ack','white_check_mark'])
        self.assertTrue(any(m=='reactions.remove' and b['timestamp']==EVENT['messageTs'] for m,b in self.api.slack_writes))
    def test_comment_with_blank_paragraphs_roundtrips(self):
        self.plan['actions']=['reply'];self.plan['comment']='#intercom\n\nThe error was caused by duplicate configuration.\n\nThe configuration was corrected.'
        self.assertEqual(self.execute()['completed'],['reply'])
    def test_jira_identity_mismatch_blocks_both_actions(self):
        self.api.jira_account='someone-else'
        self.assertEqual(self.execute()['error'],'jira_actor_mismatch')
        self.assertFalse(self.api.writes)
    def test_unrelated_ticket_is_not_guessed(self):
        self.plan['issue']='ENG-999'
        self.assertEqual(self.execute()['error'],'ticket_not_in_slack_thread')
        self.assertFalse(self.api.writes)
    def test_old_slack_key_resolves_to_canonical_issue_and_deduplicates(self):
        self.api.issue['key']='NEW-456'
        result=self.execute()
        self.assertEqual(result['issue'],'NEW-456')
        self.assertEqual(result['completed'],['reply','close'])
        self.plan.update(issue='NEW-456',sourceIssue='ENG-123')
        self.assertEqual(self.execute()['completed'],['reply','close'])
        self.assertEqual(len(self.api.writes),2)
    def test_canonical_target_accepts_only_server_verified_alias(self):
        self.api.issue['key']='NEW-456';self.plan.update(issue='NEW-456',sourceIssue='ENG-123')
        self.assertEqual(self.execute()['completed'],['reply','close'])
    def test_source_key_of_different_issue_is_not_a_migration(self):
        original=self.api.jira
        def jira(path,*args):
            result=original(path,*args)
            if path=='issue/NEW-456':result.update(id='456',key='NEW-456')
            return result
        self.api.jira=jira;self.plan.update(issue='NEW-456',sourceIssue='ENG-123')
        self.assertEqual(self.execute()['error'],'jira_alias_mismatch')
        self.assertFalse(self.api.writes)
    def test_unmentioned_source_alias_is_rejected(self):
        self.plan['sourceIssue']='OLD-999'
        self.assertEqual(self.execute()['error'],'ticket_not_in_slack_thread')
        self.assertFalse(self.api.writes)
    def test_terminal_textarea_normalizes_plain_text_to_adf(self):
        self.plan['actions']=['close'];self.plan['fields']['solution']='Use the Closed conversation filter.'
        self.api.transition['fields']['solution']={'name':'Solution','schema':{'custom':'jira:textarea'},'required':True}
        self.assertEqual(self.execute()['completed'],['close'])
        saved=self.api.writes[0][1]['fields']['solution']
        self.assertEqual(saved['type'],'doc');self.assertEqual(t.text_of(saved),self.plan['fields']['solution'])
    def test_system_rich_text_normalizes_plain_text_to_adf(self):
        self.plan['actions']=['close'];self.plan['fields']['description']='Verified root cause and solution.'
        self.api.transition['fields']['description']={'schema':{'type':'string','system':'description'}}
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.api.writes[0][1]['fields']['description']['type'],'doc')
    def test_wrong_field_json_types_are_blocked_before_post(self):
        examples=[('option',[{'id':'1'}]),('array',{'id':'1'}),('string',{'value':'text'})]
        for kind,value in examples:
            with self.subTest(kind=kind):
                self.plan['actions']=['close'];self.plan['fields']['field']=value
                self.api.transition['fields']['field']={'schema':{'type':kind}}
                self.assertEqual(self.execute()['errors']['close'],'invalid_field_type:field')
                self.assertFalse(self.api.writes)
    def test_transition_field_without_set_operation_is_blocked(self):
        self.plan['actions']=['close'];self.api.transition['fields']['resolution']['operations']=[]
        self.assertEqual(self.execute()['errors']['close'],'field_not_writable:resolution')
        self.assertFalse(self.api.writes)
    def test_existing_readonly_required_field_does_not_get_resubmitted(self):
        self.plan['actions']=['close'];self.plan['fields']={}
        self.api.issue['fields']['resolution']={'id':'1'}
        self.api.transition['fields']['resolution']['operations']=[]
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertNotIn('resolution',self.api.writes[0][1]['fields'])
    def test_array_string_options_remain_supported(self):
        self.plan['actions']=['close'];self.plan['fields']['tags']=['one']
        self.api.transition['fields']['tags']={'schema':{'type':'array'},'allowedValues':['one','two']}
        self.assertEqual(self.execute()['completed'],['close'])
    def test_malformed_rich_text_document_is_blocked_before_post(self):
        self.plan['actions']=['close'];self.plan['fields']['solution']={'text':'not an ADF document'}
        self.api.transition['fields']['solution']={'schema':{'type':'string','custom':'jira:textarea'}}
        self.assertEqual(self.execute()['errors']['close'],'invalid_field_document:solution')
        self.assertFalse(self.api.writes)
    def test_cascading_option_must_belong_to_selected_parent(self):
        meta={'schema':{'type':'option-with-child'},'allowedValues':[
            {'id':'parent1','value':'First','children':[{'id':'child1','value':'One'}]},
            {'id':'parent2','value':'Second','children':[{'id':'child2','value':'Two'}]}]}
        self.api.transition['fields']['reason']=meta
        self.plan['actions']=['close'];self.plan['fields']['reason']={'id':'parent1','child':{'id':'child2'}}
        self.assertEqual(self.execute()['errors']['close'],'invalid_field_option:reason')
        self.assertFalse(self.api.writes)
        self.plan['fields']['reason']['child']={'id':'child1'}
        self.assertEqual(self.execute()['completed'],['close'])
    def test_parent_without_children_rejects_invented_child(self):
        self.api.transition['fields']['reason']={'schema':{'type':'option-with-child'},'allowedValues':[{'id':'parent'}]}
        self.plan['actions']=['close'];self.plan['fields']['reason']={'id':'parent','child':{'id':'invented'}}
        self.assertEqual(self.execute()['errors']['close'],'invalid_field_option:reason')
        self.assertFalse(self.api.writes)
    def test_dry_run_does_not_claim_server_validators_passed(self):
        result=t.run(self.api,EVENT,self.plan,self.journal)
        self.assertEqual(result['receipts']['close']['serverValidation'],'not_executed')
        self.assertFalse(self.api.writes)
    def test_same_text_existing_rich_text_is_preserved(self):
        self.plan['actions']=['close'];self.plan['fields']['solution']='Existing solution.'
        self.api.transition['fields']['solution']={'name':'Solution','schema':{'custom':'jira:textarea'}}
        self.api.issue['fields']['solution']=t.adf('Existing solution.')
        self.api.issue['fields']['solution']['content'][0]['content'][0]['marks']=[{'type':'strong'}]
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertNotIn('solution',self.api.writes[0][1]['fields'])
    def test_jira_validation_details_retain_only_safe_categories(self):
        original=self.api.jira
        def jira(path,method='GET',body=None):
            if method=='POST':raise t.Blocked('http_400',{'fields':{'body':'invalid_document'},'categories':[]})
            return original(path,method,body)
        self.api.jira=jira;self.plan['actions']=['reply']
        result=self.execute()
        self.assertEqual(result['errors']['reply'],'http_400')
        self.assertEqual(result['validation']['reply']['fields'],{'body':'invalid_document'})
    def test_close_requires_no_root_cause_or_deployment(self):
        self.plan['actions']=['close']; self.plan.pop('rootCause'); self.plan.pop('comment')
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertTrue(self.api.writes[0][0].endswith('/transitions'))
    def test_reply_does_not_close(self):
        self.plan['actions']=['reply']
        self.assertEqual(self.execute()['completed'],['reply'])
        self.assertEqual(len(self.api.writes),1)
    def test_other_sender_and_app_and_changed_message_rejected_before_ack(self):
        for change in [dict(user='U2'),dict(app_id='A1'),dict(bot_id='B1'),dict(text='<@U1> do not close ENG-123')]:
            with self.subTest(change=change):
                self.api.messages[-1]={**dict(ts=EVENT['messageTs'],user='U1',text=EVENT['text']),**change}
                with self.assertRaises(t.Blocked):self.execute()
                self.assertFalse(self.api.writes or self.api.slack_writes)
    def test_missing_cause_blocks_reply_but_requested_close_can_succeed(self):
        self.plan['rootCause']['confirmed']=False
        result=self.execute()
        self.assertEqual(result['completed'],['close'])
        self.assertEqual(result['errors']['reply'],'root_cause_not_confirmed')
        self.assertFalse(any(m=='reactions.add' and b['name']=='white_check_mark' for m,b in self.api.slack_writes))
    def test_missing_required_field_reports_partial_reply(self):
        self.plan['fields']={}
        result=self.execute()
        self.assertEqual(result['completed'],['reply'])
        self.assertIn('missing_required_field:resolution',result['errors']['close'])
        self.assertTrue(result['notification_pending'])
        self.assertFalse(any(m=='chat.postMessage' for m,b in self.api.slack_writes))
        t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        failure=next(b['text'] for m,b in self.api.slack_writes if m=='chat.postMessage')
        self.assertIn('已发送 #intercom 评论',failure)
        self.assertEqual(len(self.api.writes),1)
    def test_non_english_comment_is_blocked(self):
        self.plan['actions']=['reply'];self.plan['comment']='#intercom\n原因是重复配置'
        self.assertEqual(self.execute()['errors']['reply'],'jira_content_must_be_english')
        self.assertFalse(self.api.writes)
    def test_non_english_new_field_is_blocked(self):
        self.plan['actions']=['close'];self.plan['fields']={'resolution':{'name':'已修复'}}
        self.assertEqual(self.execute()['errors']['close'],'jira_content_must_be_english')
    def test_fabricated_slack_quote_is_blocked(self):
        self.plan['actions']=['reply'];self.plan['rootCause']['evidence'][0]['quote']='invented'
        self.assertEqual(self.execute()['errors']['reply'],'slack_evidence_mismatch')
    def test_codex_evidence_is_rechecked_against_live_body(self):
        class Client:
            def call(self,method,params):
                return {'data':[{'id':'turn','items':[{'id':'item','type':'agentMessage','text':'ENG-123: Duplicate configuration caused the error.'}]}]}
            def close(self):pass
        self.plan['actions']=['reply']
        self.plan['rootCause']['evidence']=[dict(source='codex',threadId='chat',turnId='turn',itemId='item',quote='Duplicate configuration caused the error.')]
        with patch.object(t,'Codex',return_value=Client()):
            self.assertEqual(self.execute()['completed'],['reply'])
    def test_fabricated_codex_quote_does_not_pass(self):
        class Client:
            def call(self,method,params):
                return {'data':[{'id':'turn','items':[{'id':'item','type':'agentMessage','text':'ENG-123 remains under investigation.'}]}]}
            def close(self):pass
        self.plan['actions']=['reply']
        self.plan['rootCause']['evidence']=[dict(source='codex',threadId='chat',turnId='turn',itemId='item',quote='Root cause found.')]
        with patch.object(t,'Codex',return_value=Client()):
            self.assertEqual(self.execute()['errors']['reply'],'codex_evidence_mismatch')
        self.assertFalse(self.api.writes)
    def test_unknown_transition_result_recovers_without_reclosing(self):
        self.plan['actions']=['close'];self.api.after_post_error=True
        self.assertIn('error',self.execute())
        self.api.after_post_error=False
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(len(self.api.writes),1)
    def test_unknown_comment_result_recovers_without_duplicate(self):
        self.plan['actions']=['reply'];self.api.after_post_error=True
        self.assertIn('error',self.execute())
        self.api.after_post_error=False
        self.assertEqual(self.execute()['completed'],['reply'])
        self.assertEqual(len(self.api.writes),1)
    def test_unknown_comment_without_remote_record_is_not_reposted(self):
        self.plan['actions']=['reply'];self.api.fail_post='network_or_response_unknown'
        self.assertIn('error',self.execute())
        self.api.fail_post=None
        self.assertEqual(self.execute()['errors']['reply'],'ambiguous_comment_write')
        self.assertFalse(self.api.writes)
    def test_http_rejection_can_retry(self):
        self.plan['actions']=['reply'];self.api.fail_post='http_400'
        self.assertIn('error',self.execute());self.api.fail_post=None
        self.assertEqual(self.execute()['completed'],['reply'])
        self.assertFalse(any(m=='chat.postMessage' for m,b in self.api.slack_writes))
    def test_final_failure_is_explicit_idempotent_and_does_not_write_jira(self):
        self.plan['fields']={};self.execute()
        count=len(self.api.writes)
        for _ in range(2):
            result=t.finalize_failure(self.api,EVENT,self.plan,self.journal)
            self.assertFalse(result['notification_pending'])
        self.assertEqual(len(self.api.writes),count)
        self.assertEqual(sum(m=='chat.postMessage' for m,b in self.api.slack_writes),1)
        self.assertTrue(any(m=='reactions.remove' and b['name']=='ack' for m,b in self.api.slack_writes))
    def test_failure_finalization_requires_last_executed_plan(self):
        with self.assertRaisesRegex(t.Blocked,'current_execution_result_required'):
            t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        self.plan['fields']={};self.execute();self.plan['fields']={'resolution':{'id':'1'}}
        with self.assertRaisesRegex(t.Blocked,'current_execution_result_required'):
            t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        self.assertFalse(any(m=='chat.postMessage' for m,b in self.api.slack_writes))
    def test_unknown_applied_close_cannot_emit_stale_failure(self):
        self.plan['actions']=['close'];self.api.after_post_error=True;self.execute()
        with self.assertRaisesRegex(t.Blocked,'outcome_needs_reconciliation'):
            t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        self.assertFalse(any(m=='chat.postMessage' for m,b in self.api.slack_writes))
    def test_unknown_applied_comment_cannot_emit_stale_failure(self):
        self.plan['actions']=['reply'];self.api.after_post_error=True;self.execute()
        with self.assertRaisesRegex(t.Blocked,'outcome_needs_reconciliation'):
            t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        self.assertFalse(any(m=='chat.postMessage' for m,b in self.api.slack_writes))
    def test_success_corrects_previous_final_failure_without_new_message(self):
        self.plan['fields']={};self.execute()
        t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        self.plan['fields']={'resolution':{'id':'1'}}
        self.assertEqual(self.execute()['completed'],['reply','close'])
        self.assertEqual(sum(m=='chat.postMessage' for m,b in self.api.slack_writes),1)
        update=next(b for m,b in self.api.slack_writes if m=='chat.update')
        self.assertEqual(update['ts'],'200.000001');self.assertIn('全部完成',update['text'])
        self.assertEqual(len(self.api.writes),2)
    def test_unknown_failure_notice_is_not_reposted_and_success_reconciles_it(self):
        self.plan['fields']={};self.execute()
        original=self.api.slack
        def slack(method,body):
            result=original(method,body)
            if method=='chat.postMessage':raise t.Blocked('network_or_response_unknown')
            return result
        self.api.slack=slack
        with self.assertRaises(t.Blocked):t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        with self.assertRaisesRegex(t.Blocked,'failure_notice_outcome_unknown'):
            t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        self.api.slack=original;self.plan['fields']={'resolution':{'id':'1'}}
        self.assertEqual(self.execute()['completed'],['reply','close'])
        self.assertEqual(sum(m=='chat.postMessage' for m,b in self.api.slack_writes),1)
        self.assertTrue(any(m=='chat.update' for m,b in self.api.slack_writes))
    def test_already_resolved_is_noop(self):
        self.plan['actions']=['close'];self.api.issue['fields']['status']=dict(id='2',name='Resolved')
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertFalse(self.api.writes)
    def test_duplicate_terminal_choices_require_selection(self):
        original=self.api.jira
        def jira(path,*args):
            if '/transitions?' in path:return dict(transitions=[self.api.transition,{**self.api.transition,'id':'10','to':dict(id='3',name='Closed')}])
            return original(path,*args)
        self.api.jira=jira;self.plan['actions']=['close']
        self.assertEqual(self.execute()['errors']['close'],'terminal_transition_missing_or_ambiguous')
    def test_exact_field_option_and_existing_field_preservation(self):
        self.plan['actions']=['close'];self.api.issue['fields']['resolution']={'id':'3','name':'Other'}
        self.assertEqual(self.execute()['errors']['close'],'existing_field_would_change')
    def test_jira_rich_object_readback_uses_subset(self):
        original=self.api.jira
        def jira(path,*args):
            result=original(path,*args)
            if result.get('fields',{}).get('resolution'): result['fields']['resolution']['self']='https://jira.test/1'
            return result
        self.api.jira=jira
        self.assertEqual(self.execute()['completed'],['reply','close'])
    def test_ticket_lock_blocks_concurrent_process(self):
        another=t.Journal(self.temp.name)
        try:
            with self.journal.lock([self.api.base,'123']):
                with self.assertRaisesRegex(t.Blocked,'ticket_lock_busy'):
                    with another.lock([self.api.base,'123']):pass
        finally:another.close()

class BugApi(FakeApi):
    def __init__(self, status='In Progress'):
        super().__init__()
        self.issue['fields']['issuetype']={'name':'Bug Online'}
        self.issue['fields']['status']={'id':{'Open':'open','In Progress':'dev','In Testing':'testing'}[status],'name':status}
        self.issue['names']={'root':'Root Cause','solution':'Solution','devResult':'Dev Result'}
        self.issue['fields'].update(root=None,solution=None,devResult=None)
        self.stages={
            'Open':[{'id':'start-id','name':'Start Dev','to':{'id':'dev','name':'In Progress'},'fields':{}}],
            'In Progress':[{'id':'fix-id','name':'Bug Fixed','to':{'id':'testing','name':'In Testing'},'fields':{
                'root':{'name':'Root Cause','required':False,'schema':{'custom':'jira:textarea'}},
                'solution':{'name':'Solution','required':False,'schema':{'custom':'jira:textarea'}},
                'devResult':{'name':'Dev Result','required':False,'schema':{'type':'option-with-child'},'allowedValues':[{'id':'done-option','value':'Done'}]}}}],
            'In Testing':[{'id':'pass-id','name':'Pass Test','to':{'id':'resolved','name':'Resolved'},'fields':{}}]}
        self.fail_stage=None
        self.unknown_stage=None
        self.unknown_applied=True
    def jira(self,path,method='GET',body=None):
        if '/transitions?' in path:
            return {'transitions':copy.deepcopy(self.stages.get(self.issue['fields']['status']['name'],[]))}
        if method=='POST' and path.endswith('/transitions'):
            transition=next(t for t in self.stages.get(self.issue['fields']['status']['name'],[]) if t['id']==body['transition']['id'])
            if self.fail_stage==transition['id']:raise t.Blocked('http_400')
            if self.unknown_stage==transition['id'] and not self.unknown_applied:
                self.writes.append((path,body));raise t.Blocked('network_or_response_unknown')
            self.writes.append((path,body))
            self.issue['fields'].update(copy.deepcopy(body['fields']))
            self.issue['fields']['status']=copy.deepcopy(transition['to'])
            if self.unknown_stage==transition['id']:raise t.Blocked('network_or_response_unknown')
            return {}
        return super().jira(path,method,body)

class BugWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.journal=t.Journal(self.temp.name)
        self.api=BugApi()
        self.plan={'issue':'ENG-123','actions':['close'],'requestConfirmed':True,'requestText':EVENT['text'],
                   'targetStatus':'Resolved','fields':{},'bugFix':{
                       'devResult':'Done','rootCause':'The timeline used stale staff data.',
                       'solution':'Read current card staff when rendering the timeline.'}}
    def tearDown(self):self.journal.close();self.temp.cleanup()
    def execute(self):return t.run(self.api,EVENT,self.plan,self.journal,True)
    def transitions(self):return [b['transition']['id'] for p,b in self.api.writes if p.endswith('/transitions')]
    def test_in_progress_resolves_in_two_steps_with_required_fields(self):
        result=self.execute()
        self.assertEqual(result['completed'],['close'])
        self.assertEqual(self.transitions(),['fix-id','pass-id'])
        self.assertEqual(self.api.issue['fields']['status']['name'],'Resolved')
        first=self.api.writes[0][1]['fields']
        self.assertEqual(first['devResult'],{'id':'done-option'})
        self.assertEqual(t.text_of(first['root']),self.plan['bugFix']['rootCause'])
        self.execute();self.assertEqual(self.transitions(),['fix-id','pass-id'])
    def test_open_uses_only_named_start_dev_before_bug_fixed(self):
        self.api=BugApi('Open')
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.transitions(),['start-id','fix-id','pass-id'])
    def test_in_testing_only_resolves_and_does_not_repeat_bug_fixed(self):
        self.api=BugApi('In Testing');self.plan.pop('bugFix')
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.transitions(),['pass-id'])
    def test_bug_report_qa_uses_direct_resolve_without_dev_fields(self):
        self.api.issue['fields']['issuetype']={'name':'Bug Report'}
        self.api.issue['fields']['status']={'id':'qa','name':'QA Investigating'}
        self.api.stages['QA Investigating']=[{'id':'resolve-id','name':'Resolve Issue',
            'to':{'id':'resolved','name':'Resolved'},'fields':{
                'resolution':{'schema':{'type':'resolution'},'required':True,'operations':['set'],
                              'allowedValues':[{'id':'done','name':'Done'}]},
                'reason':{'schema':{'type':'option-with-child'},'required':False,'operations':['set'],
                          'allowedValues':[{'id':'ux','value':'Misleading Product Experience'}]},
                'solution':{'schema':{'type':'string','custom':'jira:textarea'},'required':False,'operations':['set']}}}]
        self.plan.pop('bugFix')
        self.plan['fields']={'resolution':{'id':'done'},'reason':{'id':'ux'},'solution':'Use the Closed conversation filter.'}
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.transitions(),['resolve-id'])
        self.assertEqual(self.api.writes[0][1]['fields']['solution']['type'],'doc')
    def test_dry_run_reports_conditional_route_without_mutating(self):
        result=t.run(self.api,EVENT,self.plan,self.journal,False)
        self.assertTrue(result['receipts']['close']['conditional'])
        self.assertEqual(result['receipts']['close']['pendingFinalState'],'Resolved')
        self.assertFalse(self.api.writes or self.api.slack_writes)
    def test_all_three_business_fields_required_even_with_false_metadata(self):
        for missing in ['devResult','rootCause','solution']:
            with self.subTest(field=missing):
                self.plan['bugFix'].pop(missing)
                result=self.execute()
                self.assertIn('missing_required_field:',result['errors']['close'])
                self.assertFalse(self.api.writes)
                self.setUp_plan_values()
    def setUp_plan_values(self):
        self.plan['bugFix']={'devResult':'Done','rootCause':'The timeline used stale staff data.',
                            'solution':'Read current card staff when rendering the timeline.'}
    def test_open_missing_facts_does_not_even_start_dev(self):
        self.api=BugApi('Open');self.plan['bugFix'].pop('rootCause')
        self.assertIn('missing_required_field:',self.execute()['errors']['close'])
        self.assertFalse(self.api.writes)
    def test_invalid_enum_and_chinese_prose_block_before_transition(self):
        self.plan['bugFix']['devResult']='Invented'
        self.assertIn('invalid_field_option:',self.execute()['errors']['close'])
        self.assertFalse(self.api.writes)
        self.setUp_plan_values();self.plan['bugFix']['solution']='修复'
        self.assertEqual(self.execute()['errors']['close'],'jira_content_must_be_english')
        self.assertFalse(self.api.writes)
    def test_missing_final_transition_reports_in_testing_partial_progress(self):
        self.api.stages['In Testing']=[]
        result=self.execute()
        self.assertEqual(result['completed'],[])
        self.assertEqual(result['receipts']['close']['steps'][-1]['status'],'In Testing')
        self.assertEqual(self.transitions(),['fix-id'])
        self.assertFalse(any(m=='reactions.add' and b['name']=='white_check_mark' for m,b in self.api.slack_writes))
        t.finalize_failure(self.api,EVENT,self.plan,self.journal)
        notice=next(b['text'] for m,b in self.api.slack_writes if m=='chat.postMessage')
        self.assertIn('In Testing',notice)
        self.assertNotIn('已完成：无',notice)
    def test_final_rejection_retry_continues_without_repeating_first_step(self):
        self.api.fail_stage='pass-id';self.assertIn('error',self.execute())
        self.assertFalse(any(m=='chat.postMessage' or m=='reactions.remove' for m,b in self.api.slack_writes))
        self.api.fail_stage=None
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.transitions(),['fix-id','pass-id'])
        self.assertFalse(any(m=='chat.postMessage' for m,b in self.api.slack_writes))
        self.assertTrue(any(m=='reactions.add' and b['name']=='white_check_mark' for m,b in self.api.slack_writes))
    def test_new_final_required_field_can_be_supplied_after_intermediate(self):
        self.api.stages['In Testing'][0]['fields']={'note':{'name':'Resolution Note','required':True}}
        result=self.execute()
        self.assertEqual(result['errors']['close'],'missing_required_field:Resolution Note')
        self.assertEqual(self.transitions(),['fix-id'])
        self.plan['fields']={'note':'Resolution requested by the owner.'}
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.transitions(),['fix-id','pass-id'])
    def test_unknown_applied_intermediate_recovers_then_resolves(self):
        self.api.unknown_stage='fix-id';self.assertIn('error',self.execute())
        self.api.unknown_stage=None
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.transitions(),['fix-id','pass-id'])
    def test_unknown_unapplied_step_is_not_blindly_retried(self):
        self.api.unknown_stage='fix-id';self.api.unknown_applied=False
        self.assertIn('error',self.execute());self.api.unknown_stage=None
        self.assertEqual(self.execute()['errors']['close'],'ambiguous_transition_write')
        self.assertEqual(self.transitions(),['fix-id'])
    def test_unknown_applied_final_step_recovers_without_repeating(self):
        self.api.unknown_stage='pass-id';self.assertIn('error',self.execute());self.api.unknown_stage=None
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(self.transitions(),['fix-id','pass-id'])
    def test_non_bug_and_unrelated_intermediate_are_not_traversed(self):
        self.api.issue['fields']['issuetype']={'name':'Task'}
        self.assertIn('error',self.execute());self.assertFalse(self.api.writes)
        self.api.issue['fields']['issuetype']={'name':'Bug Online'}
        self.api.stages['In Progress'][0]['name']='Need Clarification'
        self.assertIn('error',self.execute());self.assertFalse(self.api.writes)
    def test_edit_during_workflow_stops_after_verified_intermediate(self):
        original=self.api.jira
        def jira(path,*args):
            result=original(path,*args)
            if len(args)>0 and args[0]=='POST' and self.api.issue['fields']['status']['name']=='In Testing':
                self.api.messages[-1]['text']='Changed original instruction'
            return result
        self.api.jira=jira
        result=self.execute()
        self.assertEqual(result['errors']['close'],'trigger_changed')
        self.assertEqual(self.transitions(),['fix-id'])
        self.assertEqual(result['receipts']['close']['steps'][-1]['status'],'In Testing')
    def test_open_conflicting_existing_facts_do_not_start_dev(self):
        self.api=BugApi('Open');self.api.issue['fields']['root']=t.adf('Existing different cause.')
        self.assertEqual(self.execute()['errors']['close'],'existing_field_would_change')
        self.assertFalse(self.api.writes)
    def test_existing_values_preserved(self):
        self.api.issue['fields'].update(root=t.adf('Existing cause.'),solution=t.adf('Existing solution.'),devResult={'id':'done-option','value':'Done'})
        self.plan['bugFix']={}
        self.assertEqual(self.execute()['completed'],['close'])
        self.assertEqual(t.text_of(self.api.issue['fields']['root']),'Existing cause.')

class ApiTransportTest(unittest.TestCase):
    def test_failure_notice_attributes_both_actions_and_no_field_message(self):
        result={'issue':'ENG-123','completed':[],'errors':{'reply':'http_400','close':'http_400'},
                'validation':{'reply':{'categories':['invalid_document'],'fields':{}},
                              'close':{'categories':[],'fields':{'resolution':'required'}}}}
        text=t.failure_text(result)
        self.assertIn('回复 #intercom',text);self.assertIn('关闭工单',text)
        self.assertIn('富文本格式不符合 ADF',text);self.assertIn('resolution（缺少必填值）',text)
        self.assertNotIn('可能存在额外的必填字段',text)
    def test_http400_without_details_does_not_invent_a_workflow_cause(self):
        text=t.failure_text({'issue':'ENG-123','completed':[],'errors':{'reply':'http_400'}})
        self.assertIn('尚不能确定具体原因',text)
        self.assertNotIn('工作流',text)
    def test_http400_keeps_field_categories_without_server_text(self):
        api=t.Api({'JIRA_LOGIN':'user','JIRA_API_TOKEN':'test-token'})
        body={'errors':{'customfield_13710':'Must be a valid document. customer@example.com private-token',
                        'resolution':'You must specify a resolution.'},
              'errorMessages':['Customer secret at https://private.example/path?token=private-token']}
        error=urllib.error.HTTPError(api.base+'/rest/api/3/issue/123/transitions',400,'Bad Request',{},io.BytesIO(json.dumps(body).encode()))
        with patch.object(t.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(t.Blocked) as caught:api.jira('issue/123/transitions','POST',{})
        self.assertEqual(str(caught.exception),'http_400')
        details=caught.exception.details
        self.assertEqual(details['fields'],{'customfield_13710':'invalid_document','resolution':'required'})
        self.assertNotIn('private',json.dumps(details));self.assertNotIn('customer@',json.dumps(details))
    def test_non_json_error_body_is_not_exposed(self):
        self.assertIsNone(t.validation_details(b'<html>private-token</html>'))
    def test_slack_reads_use_query_parameters(self):
        api=t.Api({'SLACK_USER_TOKEN':'test-token'})
        with patch.object(api,'request',return_value={'ok':True}) as request:
            api.slack('conversations.replies',{'channel':'C1','ts':'100.000001','limit':100})
            args=request.call_args.args
            self.assertEqual(args[1],'GET')
            self.assertIn('channel=C1',args[0]);self.assertIn('ts=100.000001',args[0])
            self.assertIsNone(args[2])
    def test_slack_writes_keep_json_post(self):
        api=t.Api({'SLACK_USER_TOKEN':'test-token'})
        with patch.object(api,'request',return_value={'ok':True}) as request:
            api.slack('reactions.add',{'channel':'C1','timestamp':'100.000001','name':'ack'})
            self.assertEqual(request.call_args.args[1],'POST')

class ContextTest(unittest.TestCase):
    def test_body_search_and_archived_and_pages(self):
        class Client:
            def call(self,method,params):
                if method=='thread/list':
                    return dict(data=[dict(id='archived' if params['archived'] else 'active',cwd='/project',name='No ticket in title')],nextCursor=None)
                if params.get('cursor')=='older':
                    return dict(data=[dict(id='turn2',status='completed',items=[dict(id='i2',type='agentMessage',text='ENG-123 cause confirmed')])],nextCursor=None)
                return dict(data=[dict(id='turn1',status='completed',items=[dict(id='i1',type='agentMessage',text='ENG-1234 is a different ticket')])],nextCursor='older')
        result=search(Client(),'ENG-123',['/project'])
        self.assertTrue(result['complete']);self.assertEqual(len(result['threads']),2)
        self.assertEqual(result['threads'][0]['matchedItemIds'],['i2'])
        self.assertEqual(len(result['threads'][0]['records']),2)
    def test_read_only_rpc_guard(self):
        client=Codex.__new__(Codex)
        with self.assertRaisesRegex(Unavailable,'read_only_method_required'):client.call('turn/start',{})

if __name__=='__main__':unittest.main()
