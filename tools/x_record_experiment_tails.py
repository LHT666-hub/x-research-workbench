"""Append-only verification sidecars for the completed A/B half-day experiments."""
import hashlib
import json
import x_keyword_collector as c


def main():
    summary=[]
    for node in ('a','b'):
        root=c.ROOT/f'outputs/search_halves_20261012/{node}'
        for label in ('first_half','second_half'):
            directory=root/label
            target=directory/'search_end_verification.json'
            if target.exists():
                raise RuntimeError('Verification already exists; do not overwrite')
            result=json.loads((directory/'result.json').read_text(encoding='utf-8'))
            manifest=json.loads((directory/'responses_manifest.json').read_text(encoding='utf-8'))
            events=json.loads((directory/'network_events.json').read_text(encoding='utf-8'))
            coverage=result['coverage']
            pages=[]
            for index in range(len(manifest)-3,len(manifest)):
                entry=manifest[index]
                if entry['request_cursor']!=manifest[index-1]['next_cursor']:
                    raise RuntimeError('Broken cursor chain')
                matching=[e for e in events if e['event']=='response' and e['query']==coverage['query'] and e['cursor']==entry['request_cursor'] and e['status']==200]
                if not matching:
                    raise RuntimeError('Missing matching HTTP200 network event')
                path=directory/entry['file']
                pages.append({'page':index+1,'http_status':200,'query':coverage['query'],
                    'request_cursor':entry['request_cursor'],'request_cursor_matches':True,
                    'next_cursor':entry['next_cursor'],'parsed_count':entry['parsed_count'],
                    'received_at':entry['at'],'response_file':str(path.resolve()),
                    'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            record={**coverage,'account_node':node,'original_stop_reason':coverage['stop_reason'],
                'record_type':'append_only_search_end_verification','verified_at':c.now_iso(),
                'stop_reason':'validated_empty_tail','coverage_status':'search_tail_verified',
                'search_end_status':'validated_empty_tail','search_end_verified':True,
                'search_end_scope':'exact_query_this_account_not_entire_day_or_platform_all_posts',
                'platform_all_posts_proven':False,
                'source_result_file':str((directory/'result.json').resolve()),
                'search_end_evidence':{'policy':c.EMPTY_TAIL_POLICY,'required_empty_responses':3,'responses':pages}}
            if not c.validated_empty_tail(record):
                raise RuntimeError('Raw response verification failed')
            c._write_json_atomic(target,record)
            summary.append({'account':node,'segment':label,'count':record['count'],'state':'validated_empty_tail','path':str(target.resolve())})
    c._write_json_atomic(c.ROOT/'outputs/search_halves_20261012/search_end_verification_summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    main()
