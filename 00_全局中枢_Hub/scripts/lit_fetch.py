#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/lit_fetch.py
轻量文献元数据、摘要与 BibTeX 检索工具（灵感源自 OpenResearch Literature Primitives）：
- 支持按 DOI 精确核验与拉取 BibTeX（Crossref API）
- 支持按关键词检索 PubMed / EuropePMC（含 bioRxiv / medRxiv 预印本与期刊论文）
- 零第三方依赖（纯 Python 标准库），可直接供 citation-check / evidence-driven-writing 调用
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from typing import Any, Dict, List


def fetch_by_doi(doi: str) -> Dict[str, Any]:
    clean = doi.strip().removeprefix("https://doi.org/").removeprefix("http://doi.org/").removeprefix("doi:")
    q_doi = urllib.parse.quote(clean, safe="/")
    meta_url = f"https://api.crossref.org/works/{q_doi}"
    req = urllib.request.Request(meta_url, headers={"User-Agent": "ResearchHub-LitFetch/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        msg = json.loads(resp.read().decode("utf-8")).get("message", {})

    # 拉取标准 BibTeX
    bib_req = urllib.request.Request(
        f"https://doi.org/{q_doi}",
        headers={"Accept": "application/x-bibtex; charset=utf-8", "User-Agent": "ResearchHub-LitFetch/1.0"},
    )
    bibtex = ""
    try:
        with urllib.request.urlopen(bib_req, timeout=15) as bresp:
            bibtex = bresp.read().decode("utf-8", errors="ignore").strip()
    except Exception:
        bibtex = ""

    if bibtex and not bibtex.lstrip().startswith("@"):
        bibtex = ""

    authors = [
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in (msg.get("author") or [])
    ]
    issued = ((msg.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return {
        "doi": clean,
        "title": ((msg.get("title") or [""])[0]),
        "container_title": ((msg.get("container-title") or [""])[0]),
        "year": issued,
        "volume": msg.get("volume", ""),
        "issue": msg.get("issue", ""),
        "page": msg.get("page", ""),
        "authors": authors,
        "abstract": msg.get("abstract", ""),
        "url": msg.get("URL", f"https://doi.org/{clean}"),
        "bibtex": bibtex,
    }


def search_europe_pmc(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """通过 EuropePMC 统一检索 PubMed + bioRxiv/medRxiv 论文与摘要。"""
    params = urllib.parse.urlencode({
        "query": query,
        "format": "json",
        "pageSize": max(1, min(limit, 25)),
        "resultType": "core",
    })
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "ResearchHub-LitFetch/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = (data.get("resultList") or {}).get("result") or []
    out: List[Dict[str, Any]] = []
    for r in results:
        out.append({
            "title": r.get("title", ""),
            "authorString": r.get("authorString", ""),
            "journal": r.get("journalTitle") or r.get("bookOrReportDetails", {}).get("publisher", "Preprint"),
            "year": r.get("pubYear", ""),
            "doi": r.get("doi", ""),
            "pmid": r.get("pmid", ""),
            "source": r.get("source", ""),
            "abstract": (r.get("abstractText") or "")[:1200],
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="ResearchHub 轻量文献检索与 DOI/BibTeX 核验工具")
    parser.add_argument("--doi", help="按 DOI 精确拉取元数据与标准 BibTeX")
    parser.add_argument("--query", "-q", help="按关键词检索 PubMed + bioRxiv 文献与摘要")
    parser.add_argument("--limit", type=int, default=5, help="检索返回条数（默认 5）")
    parser.add_argument("--bibtex-only", action="store_true", help="仅输出 BibTeX 条目")
    args = parser.parse_args()

    if args.doi:
        info = fetch_by_doi(args.doi)
        if args.bibtex_only and info.get("bibtex"):
            print(info["bibtex"])
        else:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if args.query:
        items = search_europe_pmc(args.query, limit=args.limit)
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
