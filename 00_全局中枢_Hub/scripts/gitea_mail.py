"""Durable inbox and optimistic concurrency checks for host-mediated Gitea replies."""
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager, closing


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def database(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(root/'mail.sqlite', timeout=30)) as db, db:
        db.executescript('''CREATE TABLE IF NOT EXISTS events (
          ticket TEXT, id TEXT, rev TEXT, author TEXT, body TEXT, ts TEXT, status TEXT,
          PRIMARY KEY(ticket,id));
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, comment INTEGER);''')
        yield db


def ingest(root, records, user):
    with database(root) as db:
        first = not db.execute("SELECT 1 FROM meta WHERE key='success'").fetchone()
        changed = set()
        for ts, ident, ticket, author, body in records:
            if author.casefold() == user.casefold():
                continue
            rev = fingerprint([author, body]); old = db.execute('SELECT rev FROM events WHERE ticket=? AND id=?',(ticket,str(ident))).fetchone()
            if old and old[0] == rev:
                continue
            db.execute('INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?)',
                       (ticket,str(ident),rev,author,body,ts,'历史待核对' if first else '待处理'))
            if not first: changed.add(str(ticket))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('success',?)", (stamp(),))
        db.execute("DELETE FROM meta WHERE key='error'")
        return sorted(changed)


def sync_error(root, exc):
    with database(root) as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('error',?)",(stamp()+' '+type(exc).__name__,))


def list_pending(root, project):
    with database(root) as db:
        meta=dict(db.execute('SELECT key,value FROM meta'))
        lines=['# Gitea 待处理消息', '最近成功同步 '+meta.get('success','尚无记录')]
        if 'error' in meta: lines.append('本次刷新失败，以下为已有记录。'+meta['error'])
        for ticket, ident, rev, author, body, ts, status in db.execute(
                "SELECT * FROM events WHERE status!='已处理' ORDER BY ticket,ts DESC,id"):
            event_path=publish(project,'event',f'{ticket} / {ident} · @{author}\n\n{body}\n')
            lines.extend(['',f'## {ticket} / {ident} · @{author} · {status} · {ts}', f'event_rev `{rev}`',body[:160].replace('\n',' '), event_path])
        return '\n'.join(lines)+'\n'


def mark(root, job):
    status={'read':'已读待处理','done':'已处理'}[job['status']]
    with database(root) as db:
        row=db.execute('UPDATE events SET status=? WHERE ticket=? AND id=? AND rev=?',
            (status,str(job['ticket']),str(job['event_id']),job['event_rev']))
        if not row.rowcount: raise ValueError('事件已变化，请重新读取待处理清单')
    return True,status


def publish(project, prefix, contents):
    folder=project/'02_实验与验证'/'_inbox_copy'
    if folder.resolve()!=folder.absolute(): raise ValueError('收件目录越界')
    folder.mkdir(exist_ok=True)
    raw=contents.encode(); path=folder/(prefix+'-'+hashlib.sha256(raw).hexdigest()+'.md')
    with tempfile.NamedTemporaryFile(dir=folder,prefix='.incoming-') as tmp:
        tmp.write(raw); tmp.flush(); os.fchmod(tmp.fileno(),0o644)
        try: os.link(tmp.name,path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes()!=raw: raise ValueError('快照文件冲突')
    return str(path.relative_to(project))


def context(get, number, pages):
    endpoint=f'issues/{number}'
    def issue():
        code, obj=get(endpoint)
        if code!=200 or not isinstance(obj,dict) or int(obj['number'])!=number or obj.get('pull_request'):
            raise ValueError(f'无法核验工单 #{number} (HTTP {code})')
        return obj
    item=issue()
    rows=sorted(pages(get,endpoint+'/comments',item['comments']),key=lambda c:int(c['id'])) if item['comments'] else []
    end=issue()
    if len(rows)!=end['comments'] or item!=end: raise ValueError('读取期间工单有变化，请重新读取')
    return dict(number=number,title=end['title'],state=end['state'],body=end.get('body') or '',
                comments=[dict(id=c['id'],author=c['user']['login'],body=c.get('body') or '',updated=c.get('updated_at')) for c in rows])


def snapshot(project, ctx, root):
    rev=fingerprint(ctx); num=ctx['number']; comments=ctx['comments']
    with database(root) as db:
        db.execute('CREATE TABLE IF NOT EXISTS snapshots (rev TEXT PRIMARY KEY,data TEXT)')
        db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?)',(rev,json.dumps(ctx,ensure_ascii=False)))
    lines=[f"# #{num} {ctx['title']} ({ctx['state']})",f'snapshot `{rev}`',ctx['body']]
    for c in comments: lines.extend(['',f"## 评论 {c['id']} @{c['author']} {c['updated']}",c['body']])
    path=publish(project,f'issue-{num}', '\n'.join(lines)+'\n')
    return f"工单 #{num} · 最新评论 {comments[-1]['id'] if comments else '无'}\nsnapshot `{rev}`\n{path}"


def guarded_reply(project, root, job, user, body, fetch, post):
    num=int(job['issue_number']); before=fetch(); base=job.get('snapshot'); key=fingerprint([num,user,body])
    def matching(ctx):
        return [c['id'] for c in ctx['comments'] if c['author']==user and c['body']==body]
    existing=matching(before)
    if existing: return True,f'远端已存在相同正文，评论 ID {existing[-1]}'
    if base!=fingerprint(before):
        msg=snapshot(project,before,root)
        with database(root) as db:
            row=db.execute('SELECT data FROM snapshots WHERE rev=?',(str(base),)).fetchone()
        if row:
            old=json.loads(row[0]); previous={c['id']:c for c in old['comments']}
            diff=[str(c['id']) for c in before['comments'] if c!=previous.get(c['id'])]
            msg+='\n新增或编辑评论 ID：'+(', '.join(diff) or '无；请核对正文、状态或已移除评论')
        return False,'草稿待更新，未发送。\n'+msg
    with database(root) as db:
        row=db.execute('INSERT OR IGNORE INTO attempts VALUES (?,NULL)',(key,))
        if not row.rowcount: return False,'此前请求已提交或结果待核对，本次停止重发。请核对目标工单。'
    try:
        code,response=post(body)
    except Exception:
        code,response=0,{}
    if code in (200,201) and isinstance(response,dict) and response.get('id'):
        ident=response['id']
    else:
        if code in (400,401,403,404,422,429):
            with database(root) as db: db.execute('DELETE FROM attempts WHERE key=?',(key,))
            return False,f'服务器拒绝发送，HTTP {code}'
        found=matching(fetch())
        if not found: return False,'发送结果待核对，已保留发送记录以避免重复投递。'
        ident=found[-1]
    with database(root) as db: db.execute('UPDATE attempts SET comment=? WHERE key=?',(ident,key))
    msg=f'已发送到 #{num}，评论 ID {ident}'
    try:
        after=fetch(); others=dict(after,comments=[c for c in after['comments'] if c['id']!=ident]);
        if ident not in matching(after) or others!=before: msg+='。请复核发送后的工单。\n'+snapshot(project,after,root)
    except Exception: msg+='。发送后回读暂不可用，请补读工单。'
    return True,msg
