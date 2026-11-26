"""Read-only Gitea synchronization. Publish a snapshot only after all pages succeed."""
import gitea_mail
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


def replace(path, text, mode=None):
    if path.is_file() and not path.is_symlink() and path.read_text() == text:
        if mode is not None and path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, mode='w', encoding='utf-8', delete=False) as tmp:
        tmp.write(text)
        if mode is not None:
            os.fchmod(tmp.fileno(), mode)
    os.replace(tmp.name,path)


def publish_shared(path, text, state_root):
    # A VirtioFS lookup retains the original inode in the guest VFS namei cache.
    # Always update existing files in-place so the inode never becomes unlinked (avoiding Guest ENOENT).
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity_file = state_root / ('inode-' + path.name + '.json')
    payload = text.encode('utf-8')
    if path.exists() and not path.is_symlink():
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            with os.fdopen(fd, 'r+b') as dest:
                info = os.fstat(fd)
                if stat.S_ISREG(info.st_mode):
                    if dest.read() != payload:
                        dest.seek(0)
                        dest.write(payload)
                        dest.truncate()
                        dest.flush()
                        os.fsync(fd)
                    replace(identity_file, json.dumps([info.st_dev, info.st_ino]))
                    return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fchmod(tmp.fileno(), 0o644)
        info = os.fstat(tmp.fileno())
    os.replace(tmp.name, path)
    replace(identity_file, json.dumps([info.st_dev, info.st_ino]))


def pages(get, endpoint, expected=None):
    seen = set()
    for page in range(1, 10001):
        sep = '&' if '?' in endpoint else '?'
        code, items = get(f'{endpoint}{sep}limit=50&page={page}')
        if code != 200 or not isinstance(items,list):
            raise RuntimeError(f'Gitea HTTP {code} on {endpoint.split("?")[0]}')
        if not items:
            if expected is not None and len(seen)<expected: raise RuntimeError("工单评论数与返回记录不一致")
            return
        ids = {item['id'] for item in items}
        if not ids - seen:
            raise RuntimeError('Gitea 分页没有前进')
        for item in items:
            if item['id'] not in seen:
                seen.add(item['id'])
                yield item
        if expected is not None and len(seen) >= expected: return
    raise RuntimeError('工单分页超限')


def recent_comments(records):
    ordered = sorted(records, key=lambda r: (r[0], str(r[1])), reverse=True)
    lines = ['# 与丙相关的 Gitea 工单最新评论', '', '以下是协作者的消息原文。']
    for title, rows in [('负责人最近回复', [r for r in ordered if r[3].lower() == 'p02-lead'][:3]),
                        ('最近评论', ordered[:3])]:
        lines.extend(['', '## ' + title])
        if not rows:
            lines.append('暂无评论。')
        for stamp, ident, number, author, body in rows:
            lines.extend(['', f'### #{number} · @{author} · ID {ident} · {stamp}', '', body or '（无正文）'])
    return '\n'.join(lines) + '\n'


def sync(folder, get, mine, user, notify, state_root, export=None, pr_report=None):
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_root/'sync.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = state_root/'state.json'
        previous = json.loads(state_path.read_text()) if state_path.exists() else None
        state, alerts, records = {}, [], []
        lines = ['# 与我相关的 Gitea 工单及完整评论', '', '以下为协作者撰写的资料。实验授权以本课题已批准的任务为准。']
        for item in pages(get,'issues?state=all&type=issues'):
            if item.get('pull_request'): continue
            num = str(int(item['number']))
            comments = sorted(pages(get,f'issues/{num}/comments',item.get('comments')),key=lambda c:(c.get('created_at',''),c['id'])) if item.get('comments') != 0 else []
            if not mine(item) and not any(mine(dict(user=c.get('user'),body=c.get('body'))) for c in comments): continue
            lead_comments = [str(c['id']) for c in comments if c['user']['login'].lower()=='p02-lead']
            lead_body = item.get('body') or '' if item['user']['login'].lower() == 'p02-lead' else ''
            fingerprints = {str(c['id']):hashlib.sha256((c.get('body') or '').encode()).hexdigest() for c in comments if str(c['id']) in lead_comments}
            state[num] = dict(state=item['state'],lead=lead_comments,edits=fingerprints,body=hashlib.sha256(lead_body.encode()).hexdigest())
            if previous is not None:
                old = previous.get(num,{})
                edited = any(key in fingerprints and fingerprints[key]!=val for key,val in old.get('edits',{}).items())
                if edited or old.get('body',state[num]['body'])!=state[num]['body'] or set(lead_comments)-set(old.get('lead',[])) or old.get('state')!=item['state']:
                    alerts.append('#'+num)
            lines.extend(['',f"## #{num} {item['title']}",f"状态 {item['state']} · @{item['user']['login']} · {item.get('updated_at','')}",'',str(item.get('body') or '（无正文）'),'',f'### 全部评论（{len(comments)}）'])
            records.append((
                item.get('created_at') or item.get('updated_at') or '',
                f"ISSUE-{num}",
                num,
                item['user']['login'],
                f"**[工单正文 · {item.get('state','')}] {item.get('title','')}**\n\n" + str(item.get('body') or ''),
            ))
            for c in comments:
                records.append((c.get('updated_at') or c.get('created_at') or '', c['id'], num, c['user']['login'], c.get('body') or ''))
                lines.extend(['',f"#### @{c['user']['login']} · {c.get('created_at','')} · ID {c['id']}",'',str(c.get('body') or '（空）')])

        # 同步所有与我相关的 Pull Requests（含 PR 审查结论、代码行级评论与讨论回复，解决 Problem #8）
        lines.extend(['', '---', '', '# 与我相关的 Pull Requests 审查与行级评论'])
        for pr in pages(get, 'pulls?state=all'):
            pnum = str(int(pr['number']))
            pr_comments = sorted(pages(get, f'issues/{pnum}/comments'), key=lambda c: (c.get('created_at', ''), c['id']))
            reviews = list(pages(get,f'pulls/{pnum}/reviews'))
            if not mine(pr) and not any(mine(dict(user=c.get('user'), body=c.get('body'))) for c in pr_comments) and not any(mine(dict(user=r.get('user'), body=r.get('body'))) for r in reviews):
                continue
            head_sha = ((pr.get('head') or {}).get('sha') or '')[:10]
            head_ref = (pr.get('head') or {}).get('ref') or ''
            merged = bool(pr.get('merged'))
            lines.extend([
                '',
                f"## PR #{pnum} {pr.get('title', '')}",
                f"状态 {pr.get('state', '')} (merged={merged}) · 分支 `{head_ref}` (`{head_sha}`) · @{pr.get('user', {}).get('login', '')} · {pr.get('updated_at', '')}",
                '',
                str(pr.get('body') or '（无正文）'),
            ])
            # 1) PR 正式 Review 与行级代码评论
            lines.extend(['', f'### PR 审查意见与代码行级评论（{len(reviews)} 轮审查）'])
            lead_rev_ids = []
            rev_fps = {}
            for rv in reviews:
                rid = str(rv.get('id', ''))
                ruser = (rv.get('user') or {}).get('login', 'unknown')
                rstate = rv.get('state', '')
                rtime = rv.get('submitted_at') or rv.get('updated_at') or ''
                rbody = (rv.get('body') or '').strip()
                inline_list = list(pages(get,f'pulls/{pnum}/reviews/{rid}/comments'))
                for ic in inline_list:
                    records.append((ic.get('updated_at') or ic.get('created_at') or rtime,f"INLINE-{ic['id']}",f'PR#{pnum}',(ic.get('user') or {}).get('login', ruser),f"{ic.get('path','')}:{ic.get('position','')}\n{ic.get('body','')}"))
                lines.extend(['', f"#### [Review #{rid} · {rstate}] @{ruser} · {rtime} · commit `{str(rv.get('commit_id', ''))[:10]}`"])
                if rbody:
                    lines.extend(['', rbody])
                for ic in inline_list:
                    ipath = ic.get('path', '')
                    iline = ic.get('position') or ic.get('original_position') or ''
                    ibody = (ic.get('body') or '').strip()
                    ihunk = (ic.get('diff_hunk') or '').strip()
                    lines.extend(['', f"- **代码行评论** `{ipath}:{iline}` (@{(ic.get('user') or {}).get('login', ruser)}): {ibody}"])
                    if ihunk:
                        lines.extend(['  ```diff', '\n'.join('  ' + ln for ln in ihunk.splitlines()[-6:]), '  ```'])
                combined_rv = rbody + '\n' + '\n'.join(
                    f"[代码行 `{ic.get('path','')}:{ic.get('position') or ic.get('original_position') or ''}`] {(ic.get('body') or '').strip()}"
                    for ic in inline_list
                )
                if combined_rv.strip() or rstate not in ('PENDING', ''):
                    records.append((rtime, f"RV-{rid}", f"PR#{pnum}", ruser, f"**[{rstate}]** {combined_rv.strip()}"))
                if ruser.lower() == 'p02-lead':
                    lead_rev_ids.append(rid)
                    rev_fps[rid] = hashlib.sha256(combined_rv.encode()).hexdigest()
            # 2) PR 普通讨论评论
            if pr_comments:
                lines.extend(['', f'### PR 讨论区评论（{len(pr_comments)}）'])
                for c in pr_comments:
                    records.append((c.get('updated_at') or c.get('created_at') or '', c['id'], f"PR#{pnum}", c['user']['login'], c.get('body') or ''))
                    lines.extend(['', f"#### @{c['user']['login']} · {c.get('created_at', '')} · ID {c['id']}", '', str(c.get('body') or '（空）')])
            pr_key = f"PR#{pnum}"
            state[pr_key] = dict(state=pr.get('state'), merged=merged, lead=lead_rev_ids, edits=rev_fps)
            if previous is not None and pr_key in previous:
                old_p = previous.get(pr_key, {})
                if old_p.get('state') != pr.get('state') or old_p.get('merged') != merged or set(lead_rev_ids) - set(old_p.get('lead', [])) or old_p.get('edits') != rev_fps:
                    alerts.append(pr_key)

        # 同步 Release 大文件交付与结单清理台账（解决 Problem #9）
        try:
            rel_code, rel_list = get('releases')
            if rel_code == 200 and isinstance(rel_list, list):
                tsv_lines = ['tag_name\trelease_name\tasset_name\tsize_mb\tdownload_count\tcreated_at\tlinked_issue\tissue_state\tcleanup_policy\tdownload_url']
                for rel in rel_list:
                    tname = rel.get('tag_name', '')
                    rname = (rel.get('name') or '').replace('\t', ' ')
                    rbody = rel.get('body') or ''
                    m_iss = re.search(r'#(\d+)', rbody + ' ' + rname)
                    iss_num = m_iss.group(1) if m_iss else '-'
                    iss_st = (state.get(iss_num) or {}).get('state', 'open') if iss_num != '-' else 'open'
                    policy = '结单后保留7-14天缓冲期再清理附件' if iss_st == 'open' else '已结单-进入14天清理倒计时'
                    for a in (rel.get('assets') or []):
                        sz_mb = round(float(a.get('size') or 0) / (1024 * 1024), 2)
                        tsv_lines.append(f"{tname}\t{rname}\t{a.get('name','')}\t{sz_mb}\t{a.get('download_count',0)}\t{a.get('created_at','')}\t#{iss_num}\t{iss_st}\t{policy}\t{a.get('browser_download_url','')}")
                publish_shared(folder / 'RELEASE_CLEANUP_LEDGER.tsv', '\n'.join(tsv_lines) + '\n', state_root)
        except Exception:
            pass
        detail = '\n'.join(lines) + '\n'
        digest = recent_comments(records)
        publish_shared(folder/'ISSUES_DETAIL_INBOX.md', detail, state_root)
        if export is not None:
            if export.resolve() != export or not export.parent.is_dir():
                raise ValueError('Gitea 收件箱副本目录越界或不可用')
            export.mkdir(exist_ok=True)
            publish_shared(export/'ISSUES_DETAIL_INBOX_copy.md', detail, state_root)
            publish_shared(export/'GITEA_LATEST.md', digest, state_root)
            report_cache = state_root/'pr-status-base.md'
            if pr_report is not None:
                replace(report_cache, pr_report)
            if report_cache.is_file():
                publish_shared(folder/'PR_STATUS.md', digest + '\n' + report_cache.read_text(), state_root)
        changed = gitea_mail.ingest(state_root, records, user)
        alerts = sorted(set(alerts) | {('#'+n if n.isdigit() else n) for n in changed})
        if alerts: notify('、'.join(alerts))
        replace(state_path,json.dumps(state,sort_keys=True))
        return digest if export is not None else len(state)
