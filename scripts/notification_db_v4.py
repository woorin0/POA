"""v4 operator tool; old notification_db.py must not be used after cutover.

No automatic import/approval/resend. Requeue may duplicate delivery. Never run an
old sender on the backup or restore it while a newer sender is active.
"""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['preflight','status','prepare','backup','approve','import','finish','hold','requeue'])
    p.add_argument('database');p.add_argument('--backup');p.add_argument('--actor')
    p.add_argument('--approved',action='store_true');p.add_argument('--writer-quiesced',action='store_true')
    p.add_argument('--delivery');p.add_argument('--revision',type=int);p.add_argument('--wire-digest')
    p.add_argument('--alias');p.add_argument('--source');p.add_argument('--source-row',type=int)
    p.add_argument('--exchange');p.add_argument('--account');p.add_argument('--order');p.add_argument('--root');p.add_argument('--after',default='')
    a=p.parse_args()
    from exchange.verified_pnl.migrations import preflight,verify_backup,approve_cutover,finish_cutover
    from exchange.verified_pnl.inspection import inspect,inspect_alias,inspect_legacy
    try:
        if a.action=='preflight':result=preflight(a.database)
        elif a.action=='status':
            if a.source is not None:result=inspect_legacy(a.database,a.source,a.source_row)
            elif a.alias is not None:
                ref=inspect_alias(a.database,a.alias)
                result=inspect_legacy(a.database,ref['source'],ref['source_row']) if ref['review_required'] else inspect(a.database,root=ref['root'])
            else:result=inspect(a.database,exchange=a.exchange,account=a.account,order=a.order,root=a.root,delivery=a.delivery,revision=a.revision,after=a.after)
        else:
            from exchange.verified_pnl.delivery_store import NotificationOutboxStore
            if a.action=='prepare' and a.writer_quiesced is not True:raise ValueError('writer_quiescence_required')
            s=NotificationOutboxStore(a.database,migration_mode=a.action=='prepare')
            if a.action=='prepare':result={'phase':'QUIESCED' if not s.cutover_ready() else 'CUTOVER'}
            elif a.action=='backup':result=verify_backup(s,a.backup,writer_quiesced=a.writer_quiesced)
            elif a.action=='approve':approve_cutover(s,approved=a.approved,actor=a.actor);result={'phase':'APPROVED'}
            elif a.action=='import':result={'imported':s.import_legacy_batch(40)}
            elif a.action=='finish':result={'cutover':finish_cutover(s)}
            else:result={'changed':s.manual_delivery(a.action,a.delivery,approved=a.approved,actor=a.actor,expected_revision=a.revision,expected_digest=a.wire_digest)}
        print(json.dumps(result,ensure_ascii=True));return 0
    except Exception:
        # No raw exception/identifier/secret on the operator console.
        print(json.dumps({'ok':False,'reason':'operation_rejected','action':a.action}));return 2
if __name__=='__main__':raise SystemExit(main())
