"""
One-off: load employees.csv (name, number, role) into the Firestore `employees`
collection so the backend can route calls to Sales / Nutrition.

Run locally with a service-account key:
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/serviceAccount.json
    python seed_roles.py ../employees.csv
"""
import sys
import csv
import firebase_admin
from firebase_admin import firestore

firebase_admin.initialize_app()
db = firestore.client()

path = sys.argv[1] if len(sys.argv) > 1 else "employees.csv"
with open(path, newline="", encoding="utf-8-sig") as f:
    rows = list(csv.reader(f))
header = [(h or "").strip().lower() for h in rows[0]]
def idx(c): return header.index(c) if c in header else -1
i_name, i_num, i_role = idx("emp_name"), idx("emp_number"), idx("role")

n = 0
for r in rows[1:]:
    if not r:
        continue
    num = (r[i_num].strip() if 0 <= i_num < len(r) else "")
    name = (r[i_name].strip() if 0 <= i_name < len(r) else "")
    role = (r[i_role].strip() if 0 <= i_role < len(r) else "")
    if not (num or name):
        continue
    doc_id = num or name
    db.collection("employees").document(doc_id).set(
        {"emp_name": name, "emp_number": num, "role": role})
    n += 1
print(f"Seeded {n} employees into Firestore.")
