"""
Deploy the MarineEnergyScreen Power BI project (PBIP) to a Power BI workspace
without Power BI Desktop or Git, using the Fabric REST API.

Usage (from the repository root):
    python3 -m venv .venv && source .venv/bin/activate
    pip install azure-identity requests
    python scripts/deploy_to_powerbi.py --workspace "My workspace" --eia-key YOUR_EIA_KEY

Optional:
    --project PATH   folder holding MarineEnergyScreen.SemanticModel and .Report (default: the repository root)
    --eia-key KEY    your EIA API key; written into the model's EiaApiKey parameter at upload time only
    --dry-run        build and list everything that would be uploaded, without signing in

Running it again later updates the existing semantic model and report in place.
"""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

API = "https://api.fabric.microsoft.com/v1"
SCOPE = "https://api.fabric.microsoft.com/.default"
NAME = "MarineEnergyScreen"


# ----------------------------------------------------------------------------- packaging
def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def collect_parts(item_dir: Path, skip=("definition.pbir",)) -> list:
    """Every file in the item folder becomes a definition part, except .platform, local caches and skipped files."""
    parts = []
    for f in sorted(item_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(item_dir).as_posix()
        if rel == ".platform" or rel.startswith(".pbi/") or rel in skip:
            continue
        parts.append({"path": rel, "payload": b64(f.read_bytes()), "payloadType": "InlineBase64"})
    return parts


def report_pbir(model_id: str) -> dict:
    """The local project links report -> model by folder path; in the service the link must be by model id."""
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byConnection": {"connectionString": f"semanticmodelid={model_id}"}},
    }


# ----------------------------------------------------------------------------- API helpers
class Fabric:
    def __init__(self):
        import requests
        from azure.identity import InteractiveBrowserCredential

        self.requests = requests
        print("Opening your browser to sign in with your UCI account...")
        cred = InteractiveBrowserCredential()
        self.token = cred.get_token(SCOPE).token

    def call(self, method, url, body=None):
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        r = self.requests.request(method, url if url.startswith("http") else API + url, headers=headers,
                                  data=json.dumps(body) if body is not None else None)
        if r.status_code == 202:
            return self.wait(r)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} failed ({r.status_code}):\n{r.text}")
        return r.json() if r.text else {}

    def wait(self, r):
        """Long-running operation: poll until done, then fetch the result if there is one."""
        op_url = r.headers.get("Location")
        op_id = r.headers.get("x-ms-operation-id")
        while True:
            time.sleep(int(r.headers.get("Retry-After", 3)))
            r = self.requests.get(op_url, headers={"Authorization": f"Bearer {self.token}"})
            state = r.json().get("status")
            print(f"   ... {state}")
            if state == "Succeeded":
                if op_id:
                    res = self.requests.get(f"{API}/operations/{op_id}/result",
                                            headers={"Authorization": f"Bearer {self.token}"})
                    if res.status_code == 200 and res.text:
                        return res.json()
                return {}
            if state in ("Failed", "Undefined"):
                raise RuntimeError(f"Operation failed:\n{json.dumps(r.json(), indent=2)}")

    def list_all(self, url):
        items, cont = [], url
        while cont:
            page = self.call("GET", cont)
            items += page.get("value", [])
            cont = page.get("continuationUri")
        return items


def upsert(fx: Fabric, ws_id: str, kind: str, fmt: str, parts: list) -> str:
    """Create the item, or update its definition if one with the same name already exists."""
    existing = [i for i in fx.list_all(f"/workspaces/{ws_id}/{kind}") if i["displayName"] == NAME]
    definition = {"format": fmt, "parts": parts}
    if existing:
        item_id = existing[0]["id"]
        print(f"   updating existing {kind[:-1]} {item_id}")
        fx.call("POST", f"/workspaces/{ws_id}/{kind}/{item_id}/updateDefinition", {"definition": definition})
        return item_id
    created = fx.call("POST", f"/workspaces/{ws_id}/{kind}", {"displayName": NAME, "definition": definition})
    if not created.get("id"):  # some long-running creates return no body; look the item up
        created = [i for i in fx.list_all(f"/workspaces/{ws_id}/{kind}") if i["displayName"] == NAME][0]
    return created["id"]


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, help="Name of the target workspace (e.g. your PPU workspace)")
    ap.add_argument("--project", default=None, help="Folder holding the .SemanticModel and .Report folders")
    ap.add_argument("--eia-key", help="Your EIA API key (stored only in the deployed model's parameter)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.project:
        root = Path(a.project).expanduser().resolve()
    else:
        here = Path(__file__).resolve().parent.parent
        root = here if (here / f"{NAME}.SemanticModel").is_dir() else Path(f"./{NAME}").resolve()
    sm_dir, rp_dir = root / f"{NAME}.SemanticModel", root / f"{NAME}.Report"
    if not sm_dir.is_dir() or not rp_dir.is_dir():
        sys.exit(f"Could not find {sm_dir.name} and {rp_dir.name} inside {root}.\n"
                 f"Run this from the repository root, or pass --project /path/to/the/project/folder")

    sm_parts = collect_parts(sm_dir)
    if a.eia_key:
        key = a.eia_key.strip()
        for p in sm_parts:
            if p["path"] == "definition/expressions.tmdl":
                text = base64.b64decode(p["payload"]).decode("utf-8")
                if '"PASTE_YOUR_EIA_KEY"' not in text:
                    sys.exit("Could not find the EiaApiKey placeholder - make sure you unzipped the latest MarineEnergyScreen.zip")
                p["payload"] = b64(text.replace('"PASTE_YOUR_EIA_KEY"', '"' + key.replace('"', '""') + '"').encode("utf-8"))
        print("EIA key inserted into the EiaApiKey parameter")
    else:
        print("WARNING: no --eia-key given; set EiaApiKey under the semantic model's Settings > Parameters before refreshing")
    rp_parts = collect_parts(rp_dir)
    print(f"Semantic model: {len(sm_parts)} files | Report: {len(rp_parts) + 1} files")

    if a.dry_run:
        for p in sm_parts: print("  model  ", p["path"])
        for p in rp_parts: print("  report ", p["path"])
        print("  report  definition.pbir (generated, linked to the model by id)")
        return

    fx = Fabric()

    all_ws = fx.list_all("/workspaces")
    if a.workspace.lower() == "my workspace":
        ws = [w for w in all_ws if w.get("type") == "Personal"] or \
             [w for w in all_ws if w["displayName"].lower() == "my workspace"]
    else:
        ws = [w for w in all_ws if w["displayName"].lower() == a.workspace.lower()]
    if not ws:
        sys.exit(f"No workspace called '{a.workspace}' is visible to your account. Check the exact name.")
    ws_id = ws[0]["id"]
    print(f"Workspace '{ws[0]['displayName']}' found ({ws_id})")

    print("1/2 Deploying semantic model...")
    model_id = upsert(fx, ws_id, "semanticModels", "TMDL", sm_parts)
    print(f"   semantic model id: {model_id}")

    print("2/2 Deploying report...")
    pbir = {"path": "definition.pbir", "payload": b64(json.dumps(report_pbir(model_id), indent=2).encode()),
            "payloadType": "InlineBase64"}
    report_id = upsert(fx, ws_id, "reports", "PBIR", [pbir] + rp_parts)

    print("\nDone.")
    print(f"Report:          https://app.powerbi.com/groups/{ws_id}/reports/{report_id}")
    print(f"Model settings:  https://app.powerbi.com/groups/{ws_id}/settings/datasets/{model_id}")
    print("Next: open Model settings > Data source credentials > Edit: choose Anonymous, tick Skip test connection,")
    print("      Sign in, then refresh and set a daily schedule.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"\nERROR - paste this back to Claude:\n{e}")
