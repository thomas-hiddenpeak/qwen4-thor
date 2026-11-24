"""Read the Node-created Evals run through the deployed Python SDK contract."""
import json
import ssl
import sys

import httpx2
import openai


def main():
    data = json.load(sys.stdin)
    context = ssl.create_default_context(cafile=data['ca'])
    with httpx2.Client(verify=context, trust_env=False, timeout=30) as http:
        options = dict(base_url=data['base'], http_client=http, max_retries=0)
        client = openai.OpenAI(api_key=data['key'], **options)
        peer = openai.OpenAI(api_key=data['other'], **options)
        ids = dict(eval_id=data['evaluation'], run_id=data['run'])
        run = client.evals.runs.retrieve(**ids)
        assert run.status == 'completed' and run.result_counts.total == 1
        assert run.id == data['run'] and run.per_model_usage[0].invocation_count == 1
        page = client.evals.runs.output_items.list(**ids, limit=1, status='pass')
        assert len(page.data) == 1 and page.data[0].id == data['output']
        item = client.evals.runs.output_items.retrieve(data['output'], **ids)
        assert item.model_dump(mode='json') == page.data[0].model_dump(mode='json')
        assert item.sample.output[0].content.strip() == data['answer']
        assert item.results[0].score == 1 and item.status == 'pass'
        assert item.datasource_item['id'] == data['response']
        try:
            peer.evals.runs.output_items.retrieve(data['output'], **ids)
        except openai.NotFoundError:
            foreign_status = 404
        else:
            raise AssertionError('Foreign project read a scoped output')
        assert client.evals.delete(data['evaluation']).deleted
        try:
            client.evals.retrieve(data['evaluation'])
        except openai.NotFoundError:
            deleted_status = 404
        else:
            raise AssertionError('Deleted evaluation remained accessible')
    print(json.dumps(dict(version=openai.__version__, run=run.id, output=item.id,
                          answer=data['answer'], score=item.results[0].score,
                          foreign_status=foreign_status, deleted_status=deleted_status)))


if __name__ == '__main__':
    main()
