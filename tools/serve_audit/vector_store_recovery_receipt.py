"""Check SDK request identity, owner isolation, result order and persisted metadata."""
from urllib.parse import parse_qsl


def verify(mode, raw, actor, records, previous=None):
    def require(accept, reason):
        if not accept:
            raise ValueError(reason)
    require(mode in ('prepare', 'restore') and (mode == 'prepare') == (previous is None),
            'Invalid vector recovery phase')
    ids = actor['ids']
    require(actor['sdk']=='python-3.55.1' and actor['stock_sdk_indices']==list(range(12))
            and len(raw)==12 and len(ids)==len(set(ids))==len(records)==5, 'Incomplete SDK recovery set')
    packets=iter(raw)
    traces=[]
    def check(method,path, *, owner='owner',status=200,body=None,query=None):
        row=next(packets)
        require((row['method'],row['path'],row['scope'],row['status'],row['body'])==
                (method,path,owner,status,body) and sorted(parse_qsl(row['query'],keep_blank_values=True,strict_parsing=True))==
                sorted((query or {}).items()), 'Incorrect recovery request, scope or status')
        trace=row['trace']
        require(isinstance(trace,str) and trace and trace not in traces, 'Missing or repeated request ID')
        traces.append(trace)
        return row['response']
    root='/v1/vector_stores'
    if previous is None:
        metadata={};ordering=[]
        for name,digest in records.items():
            meta={'record_sha256':digest,'revision':'608'}
            obj=check('POST',root,body={'name':name,'metadata':meta})
            require(obj['name']==name and obj['metadata']==meta and obj['object']=='vector_store' and
                    type(obj['created_at']) is int and obj['id'].startswith('vs_'), 'Project record creation differs')
            ordering.append((obj['created_at'],obj['id']));metadata[obj['id']]=dict(name=name,metadata=meta)
        require(ids==[id for _,id in sorted(ordering)],'Unordered persisted vector stores')
        changed=check('POST',root+'/'+ids[2],body={'name':None})
        require(changed['id']==ids[2] and changed['name']=='' and changed['metadata']==metadata[ids[2]]['metadata'],
                'Nullable update changed the stored metadata or returned a non-string')
        metadata[ids[2]]['name']=''
    else:
        require(previous['ids']==ids, 'Restore used new vector stores')
        metadata=previous['records']
        require({row['metadata']['record_sha256'] for row in metadata.values()}==set(records.values()),
                'Recovery record digest differed')
    value=check('GET',root+'/'+ids[2])
    require(value['id']==ids[2] and value['name']=='' and value['metadata']==metadata[ids[2]]['metadata'],
            'Cleared project record did not survive')
    for order in ('asc','desc'):
        values=ids if order=='asc' else ids[::-1]
        for end in (4,2):
            page=check('GET',root,query={'before':values[end],'limit':'2','order':order})
            expected=values[end-2:end]
            require([v['id'] for v in page['data']]==expected and page['first_id']==expected[0]
                    and page['last_id']==expected[-1] and page['has_more'] is (end>2)
                    and all(v['name']==metadata[v['id']]['name'] and v['metadata']==metadata[v['id']]['metadata']
                            for v in page['data']), 'Recovered page order, contents or cursor differs')
    denial=check('GET',root+'/'+ids[2],owner='foreign',status=404)
    require('error' in denial and 'id' not in denial and 'metadata' not in denial, 'Foreign client read metadata')
    if mode=='restore':
        for id in ids:
            value=check('DELETE',root+'/'+id)
            require(value['id']==id and value['deleted'] is True, 'Store deletion failed')
        empty=check('GET',root,query={'limit':'1'})
        require(empty['data']==[] and empty['has_more'] is False, 'Vector store cleanup incomplete')
        require(not set(previous['traces']).intersection(traces),'Repeated request after recovery')
    return dict(ids=ids,records=metadata,traces=traces,sdk=actor['sdk'],stock_sdk_indices=actor['stock_sdk_indices'])
