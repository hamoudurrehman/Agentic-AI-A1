"""Free infrastructure checks. These do NOT demonstrate an implemented agent."""
import unittest
from fastapi.testclient import TestClient
from app.main import app
from app.memory import Memory
class ScaffoldTests(unittest.TestCase):
    def test_http_contract(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get('/health').status_code,200)
            self.assertEqual(client.get('/arena/manifest').json()['arena_version'],'0.1')
            result=client.post('/arena/run',json={'task':'Test','arena_config':{'fault':'none'}})
            self.assertEqual(result.status_code,200)
            self.assertIn(result.json()['status'],['completed','needs_clarification','blocked','approval_required','tool_error','contract_error','budget_exceeded','failed'])
            self.assertEqual(client.post('/arena/run',json={'task':'  '}).status_code,422)
            self.assertEqual(client.post('/arena/run',json={'task':'Test','arena_config':{'max_steps':99}}).status_code,422)
    def test_memory_isolation_and_bound(self):
        memory=Memory()
        for i in range(10): memory.add('a',str(i),'reply')
        self.assertEqual(len(memory.get('a')),12)
        self.assertEqual(memory.get('b'),[])
        self.assertEqual(memory.get('a')[-2].type,'human')
        self.assertEqual(memory.get('a')[-1].type,'ai')
        memory.clear('a'); self.assertEqual(memory.get('a'),[])
    def test_chat_reset(self):
        with TestClient(app) as client:
            session='test-session-123456'
            result=client.post('/chat',json={'session_id':session,'task':'Test'})
            self.assertEqual(result.status_code,200)
            self.assertEqual(len(client.app.state.memory.get(session)),2)
            client.delete('/chat/'+session)
            self.assertEqual(client.app.state.memory.get(session),[])
# TODO: add domain tests with a scripted model, clarification across turns,
# injected faults, autonomy checks and budget termination. No paid test suite needed.
