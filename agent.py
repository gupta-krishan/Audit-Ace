#!/usr/bin/env python3
"""CA-Agent MVP: GST invoices, credit/debit notes, e-way bill JSON, register.
Principles: LLM only EXTRACTS text -> structured data. All maths, validation,
numbering and compliance checks are deterministic code. Human approves every doc.
Sensitive identifiers (GSTIN/PAN/phone/email/bank) are masked before any API call."""
import os, re, json, sqlite3, hashlib, argparse, datetime as dt
from decimal import Decimal as D, ROUND_HALF_UP

DB = os.environ.get("CA_DB", "ledger.db")
MODEL = os.environ.get("CA_MODEL", "claude-sonnet-5-5")
# Keep this table maintained by a human; LLM-suggested rates outside it are flagged.
VALID_RATES = {D(x) for x in ("0", "0.25", "3", "5", "18", "40")}
EWAY_LIMIT = D("50000")
q2 = lambda x: D(x).quantize(D("0.01"), ROUND_HALF_UP)

# ---------- validation ----------
def gstin_ok(g):
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", g or ""): return False
    cs = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"; s = 0
    for i, c in enumerate(g[:14]):
        p = cs.index(c) * (1 if i % 2 == 0 else 2); s += p // 36 + p % 36
    return cs[(36 - s % 36) % 36] == g[14]

def fy_of(d):  # "26-27"
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y % 100:02d}-{(y + 1) % 100:02d}", y + 1  # label, FY end year

# ---------- privacy: mask before LLM ----------
PATTERNS = [("GSTIN", r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"), ("PAN", r"\b[A-Z]{5}\d{4}[A-Z]\b"),
            ("EMAIL", r"[\w.+-]+@[\w-]+\.[\w.]+"), ("IFSC", r"\b[A-Z]{4}0[A-Z0-9]{6}\b"),
            ("PHONE", r"(?:\+91[- ]?)?\b[6-9]\d{9}\b"), ("ACCT", r"\b\d{9,18}\b")]
def mask(text):
    for tag, pat in PATTERNS:
        text = re.sub(pat, f"[{tag}]", text)
    return text

# ---------- LLM extraction (only step that touches the network) ----------
SCHEMA = {"type": "object", "required": ["party_name", "doc_type", "items"], "properties": {
    "party_name": {"type": "string"}, "doc_type": {"enum": ["invoice", "credit_note", "debit_note"]},
    "original_invoice_no": {"type": "string"}, "reason": {"type": "string"},
    "items": {"type": "array", "items": {"type": "object", "required": ["description", "qty", "rate"], "properties": {
        "description": {"type": "string"}, "hsn": {"type": "string"}, "qty": {"type": "number"},
        "unit": {"type": "string"}, "rate": {"type": "number"}, "gst_rate": {"type": "number"}}}},
    "transport": {"type": "object", "properties": {"distance_km": {"type": "integer"}, "vehicle_no": {"type": "string"},
                  "transporter_id": {"type": "string"}}}}}

def extract(text):
    import anthropic  # pip install anthropic ; set ANTHROPIC_API_KEY
    r = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=1500, tools=[{"name": "record_doc", "description": "Record the accounting document",
        "input_schema": SCHEMA}], tool_choice={"type": "tool", "name": "record_doc"},
        system="Extract fields exactly as stated. Never guess missing values; omit them. Do not compute totals or tax.",
        messages=[{"role": "user", "content": mask(text)}])
    return next(b.input for b in r.content if b.type == "tool_use")

# ---------- core engine ----------
def load_masters():
    p = os.environ.get("CA_MASTERS") or ("masters.json" if os.path.exists("masters.json") else "masters.example.json")
    return json.load(open(p))

def compute(doc, m, db):
    warn, seller = [], m["seller"]
    party = m["parties"].get(doc["party_name"].strip().lower())
    if not party: raise SystemExit(f"Party '{doc['party_name']}' not in masters.json - add it first.")
    for who, g in (("Seller", seller["gstin"]), ("Party", party["gstin"])):
        if not gstin_ok(g): warn.append(f"{who} GSTIN {g} fails checksum")
    date = dt.date.fromisoformat(doc.get("date") or dt.date.today().isoformat())
    inter = seller["gstin"][:2] != party["gstin"][:2]
    lines, tot = [], {k: D(0) for k in ("taxable", "cgst", "sgst", "igst")}
    for it in doc["items"]:
        rate = D(str(it.get("gst_rate", m.get("hsn_rates", {}).get(it.get("hsn", ""), -1))))
        if rate not in VALID_RATES: warn.append(f"'{it['description']}': GST rate {rate}% unverified - REVIEW"); rate = max(rate, D(0))
        if not it.get("hsn"): warn.append(f"'{it['description']}': HSN missing")
        taxable = q2(D(str(it["qty"])) * D(str(it["rate"])))
        c = s = i = D(0)
        if inter: i = q2(taxable * rate / 100)
        else: c = s = q2(taxable * rate / 200)
        lines.append({**it, "taxable": str(taxable), "gst_rate": str(rate), "cgst": str(c), "sgst": str(s), "igst": str(i)})
        for k, v in zip(tot, (taxable, c, s, i)): tot[k] += v
    gross = sum(tot.values()); total = gross.quantize(D("1"), ROUND_HALF_UP)
    typ = doc["doc_type"]
    if typ != "invoice":  # Section 34 CGST checks
        ref = db.execute("select date from docs where no=? and type='invoice'", (doc.get("original_invoice_no"),)).fetchone()
        if not ref: warn.append("Original invoice not found in ledger (needed for CN/DN, Sec 34)")
        else:
            _, fy_end = fy_of(dt.date.fromisoformat(ref[0]))
            if date > dt.date(fy_end, 11, 30): warn.append("Past 30 Nov following FY of original invoice: ITC reversal/ CN not allowed")
    return {"type": typ, "date": date.isoformat(), "seller": seller, "party": party, "party_name": doc["party_name"],
            "original_invoice_no": doc.get("original_invoice_no"), "reason": doc.get("reason"), "inter_state": inter,
            "lines": lines, **{k: str(q2(v)) for k, v in tot.items()}, "round_off": str(q2(total - gross)),
            "total": str(total), "transport": doc.get("transport"), "warnings": warn}

def eway_json(d):
    if d["type"] != "invoice" or D(d["total"]) <= EWAY_LIMIT: return None
    t, s, p = d.get("transport") or {}, d["seller"], d["party"]
    if not t.get("distance_km"): d["warnings"].append("Value > Rs 50,000: e-way bill needs transport distance/vehicle"); return None
    items = [{"productName": l["description"], "hsnCode": int(l["hsn"]) if l.get("hsn", "").isdigit() else 0,
              "quantity": l["qty"], "qtyUnit": l.get("unit", "NOS"), "taxableAmount": float(l["taxable"]),
              "cgstRate": float(D(l["gst_rate"]) / 2) if not d["inter_state"] else 0,
              "sgstRate": float(D(l["gst_rate"]) / 2) if not d["inter_state"] else 0,
              "igstRate": float(l["gst_rate"]) if d["inter_state"] else 0, "cessRate": 0} for l in d["lines"]]
    return {"version": "1.0.0621", "billLists": [{"userGstin": s["gstin"], "supplyType": "O", "subSupplyType": 1, "docType": "INV",
        "docNo": d["no"], "docDate": dt.date.fromisoformat(d["date"]).strftime("%d/%m/%Y"),
        "fromGstin": s["gstin"], "fromTrdName": s["name"], "fromPincode": int(s["pincode"]), "fromStateCode": int(s["gstin"][:2]), "actFromStateCode": int(s["gstin"][:2]),
        "toGstin": p["gstin"], "toTrdName": d["party_name"], "toPincode": int(p["pincode"]), "toStateCode": int(p["gstin"][:2]), "actToStateCode": int(p["gstin"][:2]),
        "totalValue": float(sum(D(l["taxable"]) for l in d["lines"])), "cgstValue": float(d["cgst"]), "sgstValue": float(d["sgst"]), "igstValue": float(d["igst"]),
        "cessValue": 0, "totInvValue": float(d["total"]), "transMode": 1, "transDistance": t["distance_km"],
        "transporterId": t.get("transporter_id", ""), "vehicleNo": t.get("vehicle_no", ""), "vehicleType": "R", "transactionType": 1, "itemList": items}]}

def html(d):
    rows = "".join(f"<tr><td>{l['description']}</td><td>{l.get('hsn','')}</td><td>{l['qty']}</td><td>{l['rate']}</td><td>{l['taxable']}</td>"
                   f"<td>{l['gst_rate']}%</td><td>{l['cgst']}/{l['sgst']}/{l['igst']}</td></tr>" for l in d["lines"])
    title = {"invoice": "TAX INVOICE", "credit_note": "CREDIT NOTE", "debit_note": "DEBIT NOTE"}[d["type"]]
    return (f"<html><body style='font-family:sans-serif'><h2>{title}</h2><p>No: {d['no']} | Date: {d['date']}"
            f"{' | Against: ' + d['original_invoice_no'] if d.get('original_invoice_no') else ''}</p>"
            f"<p><b>{d['seller']['name']}</b> GSTIN {d['seller']['gstin']}<br>To: <b>{d['party_name']}</b> GSTIN {d['party']['gstin']}</p>"
            f"<table border=1 cellpadding=4 style='border-collapse:collapse'><tr><th>Item</th><th>HSN</th><th>Qty</th><th>Rate</th><th>Taxable</th><th>GST</th><th>CGST/SGST/IGST</th></tr>{rows}</table>"
            f"<p>Taxable {d['taxable']} | CGST {d['cgst']} | SGST {d['sgst']} | IGST {d['igst']} | Round-off {d['round_off']}</p><h3>Total: Rs {d['total']}</h3>"
            f"{'<p>Reason: ' + d['reason'] + '</p>' if d.get('reason') else ''}</body></html>")

# ---------- storage + tamper-evident audit trail ----------
def conn():
    c = sqlite3.connect(DB)
    c.execute("create table if not exists docs(no text primary key,type,date,party,total,body)")
    c.execute("create table if not exists audit(id integer primary key,ts,action,doc_no,hash)")
    return c

def log(c, action, no):
    prev = (c.execute("select hash from audit order by id desc limit 1").fetchone() or ["genesis"])[0]
    ts = dt.datetime.now().isoformat(timespec="seconds")
    c.execute("insert into audit(ts,action,doc_no,hash) values(?,?,?,?)", (ts, action, no, hashlib.sha256(f"{prev}|{ts}|{action}|{no}".encode()).hexdigest()))

def next_no(c, typ, date):
    pre = {"invoice": "INV", "credit_note": "CN", "debit_note": "DN"}[typ]; fy, _ = fy_of(date)
    n = c.execute("select count(*) from docs where no like ?", (f"{pre}/{fy}/%",)).fetchone()[0] + 1
    return f"{pre}/{fy}/{n:04d}"  # <=16 chars, unique, sequential (Rule 46)

def cmd_new(a):
    m, c = load_masters(), conn()
    doc = json.load(open(a.json)) if a.json else extract(a.text)
    d = compute(doc, m, c); d["no"] = next_no(c, d["type"], dt.date.fromisoformat(d["date"]))
    ew = eway_json(d)
    print(json.dumps({k: d[k] for k in ("no", "type", "party_name", "taxable", "cgst", "sgst", "igst", "round_off", "total")}, indent=1))
    for w in d["warnings"]: print("WARNING:", w)
    if not a.yes and input("Approve & save? [y/N] ").lower() != "y": return print("Discarded.")
    os.makedirs("out", exist_ok=True); base = "out/" + d["no"].replace("/", "_")
    open(base + ".html", "w").write(html(d)); json.dump(d, open(base + ".json", "w"), indent=1)
    if ew: json.dump(ew, open(base + "_eway.json", "w"), indent=1); print("E-way bill JSON ->", base + "_eway.json")
    c.execute("insert into docs values(?,?,?,?,?,?)", (d["no"], d["type"], d["date"], d["party_name"], d["total"], json.dumps(d)))
    log(c, "APPROVED", d["no"]); c.commit(); print("Saved:", d["no"])

def cmd_register(a):  # books + GSTR-1-style summary; CN reduces, DN adds
    c = conn(); sign = {"invoice": 1, "credit_note": -1, "debit_note": 1}; agg = {}
    for (body,) in c.execute("select body from docs"):
        d = json.loads(body)
        for l in d["lines"]:
            k = l["gst_rate"]; r = agg.setdefault(k, [D(0)] * 4)
            for i, f in enumerate(("taxable", "cgst", "sgst", "igst")): r[i] += sign[d["type"]] * D(l[f])
    print(f"{'Rate%':>6} {'Taxable':>12} {'CGST':>10} {'SGST':>10} {'IGST':>10}")
    for k, r in sorted(agg.items(), key=lambda x: D(x[0])): print(f"{k:>6} " + " ".join(f"{v:>{w}}" for v, w in zip(r, (12, 10, 10, 10))))

if __name__ == "__main__":
    p = argparse.ArgumentParser(); s = p.add_subparsers(required=True)
    n = s.add_parser("new"); n.add_argument("text", nargs="?"); n.add_argument("--json"); n.add_argument("--yes", action="store_true"); n.set_defaults(f=cmd_new)
    s.add_parser("register").set_defaults(f=cmd_register)
    a = p.parse_args(); a.f(a)
