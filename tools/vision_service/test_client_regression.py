"""CPU guards for the new bounded driver, without network/model execution."""
import copy
import unittest

from tools.vision_service import client_regression as r
from tools.examples import chat_client as c


class Regression(unittest.TestCase):
    def test_whole_stage_budget_rejects_expansion(self):
        one=dict(limits=r.LIMITS)
        self.assertEqual(r.budget([one])["generations"],16)
        with self.assertRaises(RuntimeError):r.budget([one,one])
        tail=dict(limits=dict(starts=1,generations=8,controls=30,native=0,seconds=600))
        self.assertEqual(r.budget([one,tail]),r.TOTAL)
        for number in (-1,True,1.5):
            bad=dict(limits=dict(r.LIMITS,generations=number))
            with self.assertRaises(RuntimeError):r.budget([bad])

    def test_cross_language_text_media_and_final_history(self):
        output=r.schema(dict(answer=dict(type="string",description="引用原文 中文🙂")))
        for content in ("question",[dict(type="text",text="question")]):
            body=c.make_request([dict(role="user",content=content)],[],max_tokens=768)
            for final in (False,True):
                value=copy.deepcopy(body)
                if final:
                    value["messages"] += [dict(role="assistant",content=None,tool_calls=[dict(
                        id="call",type="function",function=dict(name="read",arguments="{}"))]),
                        dict(role="tool",tool_call_id="call",content='{"actual":123}')]
                    value.update(tools=[dict(type="function",function=dict(name="read"))],tool_choice="none")
                py=c.with_json_schema(value,output);js=r.js_request(value,output)
                for key in ("messages","response_format","max_tokens"):
                    self.assertEqual(py[key],js[key])


if __name__=="__main__":unittest.main()
