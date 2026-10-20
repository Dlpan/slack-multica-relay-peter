import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
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
        failure=next(b['text'] for m,b in self.api.slack_writes if m=='chat.postMessage')
        self.assertIn('reply',failure)
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

class ApiTransportTest(unittest.TestCase):
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
