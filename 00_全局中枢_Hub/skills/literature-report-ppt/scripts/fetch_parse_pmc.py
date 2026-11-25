#!/usr/bin/env python3
"""
Helper script for literature-report-ppt skill:
Fetches full-text XML from NCBI PubMed Central (PMC) by DOI or PMC ID,
parses structured sections, extracts formulas and references, and optionally
downloads high-resolution figures for Springer Nature / PMC open-access articles.
"""
import argparse
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def resolve_pmcid(doi_or_pmcid: str) -> str:
    s = doi_or_pmcid.strip()
    if s.upper().startswith("PMC"):
        return s.upper().replace("PMC", "")
    if s.isdigit():
        return s
    query = urllib.parse.quote(f"{s}[doi]")
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pmc&term={query}&retmode=json"
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    idlist = data.get("esearchresult", {}).get("idlist", [])
    if not idlist:
        raise ValueError(f"Could not find PMC ID for DOI/query: {doi_or_pmcid}")
    return idlist[0]


def get_text(elem) -> str:
    if elem is None:
        return ""
    parts = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        if child.tag == "xref":
            rid = child.attrib.get("rid", "")
            parts.append(f"[{rid}:{get_text(child)}]")
        elif child.tag in ("disp-formula", "inline-formula"):
            parts.append(" ".join(" ".join(child.itertext()).split()))
        else:
            parts.append(get_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Fetch and parse open-access paper from PMC.")
    parser.add_argument("identifier", help="DOI (e.g. 10.1038/s41467-023-36412-4) or PMC ID (e.g. PMC9974990)")
    parser.add_argument("--out-dir", default=".", help="Output directory for parsed text and figures")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    pmcid = resolve_pmcid(args.identifier)
    xml_path = os.path.join(args.out_dir, f"PMC{pmcid}.xml")
    txt_path = os.path.join(args.out_dir, f"PMC{pmcid}_parsed.txt")

    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={pmcid}&retmode=xml"
    urllib.request.urlretrieve(url, xml_path)

    tree = ET.parse(xml_path)
    root = tree.getroot()

    with open(txt_path, "w", encoding="utf-8") as f:
        title_el = root.find(".//article-meta//article-title")
        f.write(f"TITLE: {get_text(title_el)}\n\n")
        for ab in root.findall(".//abstract"):
            f.write(f"=== ABSTRACT ===\n{get_text(ab)}\n\n")
        for body in root.findall(".//body"):
            for sec in body.findall("sec"):
                f.write(f"=== SECTION: {get_text(sec.find('title'))} ===\n")
                for child in sec:
                    if child.tag == "p":
                        f.write(get_text(child) + "\n\n")
                    elif child.tag == "sec":
                        f.write(f"--- SUBSECTION: {get_text(child.find('title'))} ---\n")
                        for sub in child:
                            if sub.tag == "p":
                                f.write(get_text(sub) + "\n\n")
                            elif sub.tag == "sec":
                                f.write(f"... SUBSUBSECTION: {get_text(sub.find('title'))} ...\n")
                                for p in sub.findall("p"):
                                    f.write(get_text(p) + "\n\n")
        f.write("=== FIGURES ===\n")
        for fig in root.findall(".//fig"):
            fid = fig.attrib.get("id", "")
            label = get_text(fig.find("label"))
            cap = " ".join(get_text(fig.find("caption")).split())
            f.write(f"{fid} ({label}): {cap}\n\n")
        f.write("=== FORMULAS ===\n")
        for df in root.findall(".//disp-formula"):
            did = df.attrib.get("id", "")
            f.write(f"{did}: {' '.join(' '.join(df.itertext()).split())}\n\n")
        f.write("=== REFERENCES ===\n")
        for ref in root.findall(".//ref"):
            rid = ref.attrib.get("id", "")
            f.write(f"{rid}: {' '.join(' '.join(ref.itertext()).split())}\n")

    print(f"Successfully saved XML to {xml_path} and parsed text to {txt_path}")


if __name__ == "__main__":
    main()
