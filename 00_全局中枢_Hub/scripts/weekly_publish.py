"""Publish and assemble versioned weekly decks without rebuilding slide shapes."""
import hashlib
import json
import os
import posixpath as pp
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from lxml import etree as ET
from pptx import Presentation
from PIL import Image, ImageChops, ImageStat

WEEKLY = '04_组会与周报_Weekly'
NOTES = '02-逐页演讲稿_ELI5.md'
P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
FIELDS = ('实验目的', '怎么跑的', '指标大白话', '结果结论')


def atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        f.write(data)
    os.replace(f.name, path)


def sections(text, count):
    chunks = re.split(r'^## 第\s*(\d+)\s*页[^\n]*\n', text, flags=re.M)
    if chunks[1::2] != list(map(str, range(1, count + 1))):
        raise ValueError('ELI5 讲稿必须按 PPT 页序逐页编号：## 第 1 页 …')
    for page in chunks[2::2]:
        for field in FIELDS:
            if not re.search(r'^' + field + r'[：:]\s*\S', page, re.M):
                raise ValueError('ELI5 每页必填：' + field)
    return chunks[2::2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def latest(root):
    data = json.loads((root / 'LATEST_PPT.json').read_text())
    path = (root / data['pptx']).resolve()
    if path.parent.parent != root.resolve() or path.suffix != '.pptx' or digest(path) != data['sha256'] or digest(path.with_name(NOTES)) != data['notes_sha256']:
        raise ValueError(f'{root.parent.name} 的正式版或讲稿与登记不符，请重新发布')
    return path


def register(root, path):
    if path.resolve().parent.parent != root.resolve() or not re.fullmatch(r'\d{4}-\d\d-\d\d_.{1,20}',path.parent.name):
        raise ValueError('发布目录必须为 YYYY-MM-DD_短主题')
    notes = path.with_name(NOTES)
    pages = len(Presentation(path).slides)
    sections(notes.read_text(), pages)
    data = dict(pptx=str(path.relative_to(root)), sha256=digest(path), notes_sha256=digest(notes), slides=pages)
    atomic(root / 'LATEST_PPT.json', json.dumps(data, ensure_ascii=False, indent=2).encode())


def join_packages(paths, output):
    # Copy reachable OPC parts. Within each part all rIds remain local and unchanged.
    with zipfile.ZipFile(paths[0]) as first:
        files = {name:first.read(name) for name in first.namelist()}
    pres_name = 'ppt/presentation.xml'
    rel_name = 'ppt/_rels/presentation.xml.rels'
    types_name = '[Content_Types].xml'
    pres, rels, types = (ET.fromstring(files[n]) for n in (pres_name,rel_name,types_name))
    for idx, path in enumerate(paths[1:], 2):
        with zipfile.ZipFile(path) as src:
            src_pres = ET.fromstring(src.read(pres_name))
            if any(pres.find(P+'sldSz').get(k) != src_pres.find(P+'sldSz').get(k) for k in ('cx','cy')):
                # PowerPoint stores integer EMUs. Templates can differ by a few hundred EMUs.
                if any(abs(int(pres.find(P+'sldSz').get(k))-int(src_pres.find(P+'sldSz').get(k))) > 1000 for k in ('cx','cy')):
                    raise ValueError('画幅不同，无法原尺寸拼接')
            src_rels = {r.get('Id'):r for r in ET.fromstring(src.read(rel_name))}
            src_types = ET.fromstring(src.read(types_name))
            overrides = {n.get('PartName','').lstrip('/'):n.get('ContentType') for n in src_types if n.get('PartName')}
            defaults = {n.get('Extension'):n.get('ContentType') for n in src_types if n.get('Extension')}
            copied = {}
            def copy_part(old):
                if old in copied:
                    return copied[old]
                # Preserve standard folder structure and assign a collision-free part name.
                new = pp.join(pp.dirname(old), f'rh{idx}_'+pp.basename(old))
                if new in files:
                    raise ValueError('PPT 内部组件命名冲突')
                copied[old] = new
                files[new] = src.read(old)
                content_type = overrides.get(old) or defaults[old.rsplit('.',1)[-1]]
                ET.SubElement(types, '{'+ET.QName(types).namespace+'}Override', PartName='/'+new, ContentType=content_type)
                def relpath(name):
                    return pp.join(pp.dirname(name),'_rels',pp.basename(name)+'.rels')
                if relpath(old) in src.namelist():
                    rs = ET.fromstring(src.read(relpath(old)))
                    for r in rs:
                        if r.get('TargetMode') == 'External':
                            continue
                        target = pp.normpath(pp.join(pp.dirname(old),r.get('Target'))).lstrip('/')
                        r.set('Target',pp.relpath(copy_part(target),pp.dirname(new)))
                    files[relpath(new)] = ET.tostring(rs)
                return new
            for group, child in [('sldMasterIdLst','sldMasterId'),('sldIdLst','sldId')]:
                dest = pres.find(P+group)
                for entry in src_pres.find(P+group):
                    rel = src_rels[entry.get(R+'id')]
                    target = copy_part(pp.normpath(pp.join('ppt',rel.get('Target'))).lstrip('/'))
                    rid = f'rIdImported{idx}_{entry.get(R+"id")}'
                    ET.SubElement(rels,rel.tag,Id=rid,Type=rel.get('Type'),Target=pp.relpath(target,'ppt'))
                    # Master and layout IDs share a namespace; leave room for each master's layouts.
                    num = max(int(n.get('id')) for n in dest) + (65536 if child=='sldMasterId' else 1)
                    ET.SubElement(dest,P+child,{'id':str(num),R+'id':rid})
                    if child == 'sldMasterId':
                        master = ET.fromstring(files[target])
                        for offset, layout in enumerate(master.iter(P+'sldLayoutId'),1):
                            layout.set('id',str(num+offset))
                        files[target] = ET.tostring(master)
    for name, node in zip((pres_name,rel_name,types_name),(pres,rels,types)):
        files[name] = ET.tostring(node,encoding='UTF-8',xml_declaration=True,standalone=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in files.items():
            archive.writestr(name,data)


def render(path, folder):
    folder.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run(['soffice','-env:UserInstallation='+Path(profile).as_uri(),'--headless','--convert-to','pdf','--outdir',str(folder),str(path)], check=True,timeout=180,stdout=subprocess.PIPE)
    subprocess.run(['pdftoppm','-scale-to','1000','-png',str(folder/(path.stem+'.pdf')),str(folder/'page')],check=True,timeout=180)
    imgs = sorted(folder.glob('page-*.png'),key=lambda p:int(p.stem.split('-')[-1]))
    if len(imgs)!=len(Presentation(path).slides) or any(max(ImageStat.Stat(Image.open(i).convert('RGB')).stddev)<2 for i in imgs):
        raise ValueError('导出的页数不符或存在空白页')
    return imgs


def merge(roots, out):
    paths = [latest(root) for root in roots]
    if out in paths or out.suffix.lower()!='.pptx':
        raise ValueError('请选择独立的 .pptx 合集文件')
    out.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as temp:
        stage = Path(temp)
        source_pages, notes = [], []
        for i, path in enumerate(paths):
            source_pages.extend(render(path,stage/f'source{i}'))
            for n, body in enumerate(sections(path.with_name(NOTES).read_text(),len(Presentation(path).slides)),1):
                notes.append(f'## 第 {len(notes)+1} 页 · {roots[i].parent.name.split("_")[0]} 原第 {n} 页\n{body.strip()}\n')
        joined = stage/out.name
        join_packages(paths,joined)
        pages = render(joined,stage/'render')
        if len(pages)!=len(source_pages):
            raise ValueError('合集页数与输入不符')
        scores=[]
        for source,page in zip(source_pages,pages):
            a,b=(Image.open(p).convert('RGB') for p in (source,page))
            score=max(ImageStat.Stat(ImageChops.difference(a,b)).mean)
            if score>1:
                raise ValueError(f'{page.name} 和来源渲染差异过大 ({score:.3f})')
            scores.append(round(score,4))
        qa = out.with_name(out.stem+'_QA')
        qa.mkdir(exist_ok=True)
        for pg in pages:
            os.replace(pg,qa/pg.name)
        for old in qa.glob('page-*.png'):
            if old.name not in {pg.name for pg in pages}: old.unlink()
        atomic(qa/'report.json',json.dumps(dict(slides=len(pages),source_sha256=[digest(p) for p in paths],max_channel_mean_diff=scores),indent=2).encode())
        atomic(out.with_name(out.stem+'_ELI5.md'),('# 合集逐页演讲稿\n\n'+'\n'.join(notes)).encode())
        os.replace(joined,out)
    print(f'{out} · {len(pages)} 页 · 渲染比对通过')
