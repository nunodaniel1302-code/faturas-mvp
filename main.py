from datetime import date, datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from sqlmodel import SQLModel, Field, Session, create_engine, select
import pandas as pd

app = FastAPI(title="MVP Alertas de Faturas")

# Permitir frontend depois (se quiseres). Para MVP local, ok assim.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DB (SQLite para começar; fácil migrar para Postgres depois)
engine = create_engine("sqlite:///./app.db", echo=False)


class Company(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    nome: str


class Invoice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(index=True)

    fornecedor: str
    numero_fatura: str = Field(index=True)

    data_emissao: date
    data_vencimento: date

    valor: float
    estado: str = Field(default="EM_ABERTO", index=True)  # EM_ABERTO | PAGA


def create_db():
    SQLModel.metadata.create_all(engine)


@app.on_event("startup")
def on_startup():
    create_db()


# ---------- Helpers ----------
REQUIRED_COLS = {"fornecedor", "numero_fatura", "data_emissao", "data_vencimento", "valor"}
OPTIONAL_COLS = {"estado"}


def parse_date_safe(v) -> date:
    if pd.isna(v):
        raise ValueError("data vazia")
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    # string
    return datetime.strptime(str(v).strip(), "%Y-%m-%d").date()


def normalize_estado(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "EM_ABERTO"
    s = str(v).strip().upper()
    if s in {"PAGA", "PAGO", "PAID"}:
        return "PAGA"
    return "EM_ABERTO"


def read_file_to_df(upload: UploadFile) -> pd.DataFrame:
    filename = upload.filename.lower()
    if filename.endswith(".csv"):
        return pd.read_csv(upload.file)
    if filename.endswith(".xlsx") or filename.endswith(".xls"):
        return pd.read_excel(upload.file)
    raise HTTPException(status_code=400, detail="Formato não suportado. Usa .csv ou .xlsx")


# ---------- Endpoints ----------
@app.post("/companies", response_model=Company)
def create_company(company: Company):
    if not company.nome.strip():
        raise HTTPException(400, "Nome da empresa é obrigatório")
    with Session(engine) as session:
        session.add(company)
        session.commit()
        session.refresh(company)
        return company


@app.get("/companies", response_model=List[Company])
def list_companies():
    with Session(engine) as session:
        return session.exec(select(Company).order_by(Company.nome)).all()


@app.post("/companies/{company_id}/invoices/import")
def import_invoices(company_id: int, file: UploadFile = File(...)):
    # Verificar empresa
    with Session(engine) as session:
        company = session.get(Company, company_id)
        if not company:
            raise HTTPException(404, "Empresa não encontrada")

    df = read_file_to_df(file)
    df.columns = [c.strip().lower() for c in df.columns]

    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise HTTPException(
            400,
            detail=f"Faltam colunas obrigatórias: {', '.join(sorted(missing))}"
        )

    # Limpar e validar
    results = {"lidas": len(df), "importadas": 0, "atualizadas": 0, "erros": []}

    with Session(engine) as session:
        for i, row in df.iterrows():
            try:
                fornecedor = str(row["fornecedor"]).strip()
                numero = str(row["numero_fatura"]).strip()

                if not fornecedor or not numero:
                    raise ValueError("fornecedor/numero_fatura vazio")

                data_emissao = parse_date_safe(row["data_emissao"])
                data_venc = parse_date_safe(row["data_vencimento"])
                valor = float(row["valor"])
                estado = normalize_estado(row["estado"]) if "estado" in df.columns else "EM_ABERTO"

                # dedupe: por empresa + numero_fatura
                existing = session.exec(
                    select(Invoice).where(
                        Invoice.company_id == company_id,
                        Invoice.numero_fatura == numero
                    )
                ).first()

                if existing:
                    existing.fornecedor = fornecedor
                    existing.data_emissao = data_emissao
                    existing.data_vencimento = data_venc
                    existing.valor = valor
                    existing.estado = estado
                    session.add(existing)
                    results["atualizadas"] += 1
                else:
                    inv = Invoice(
                        company_id=company_id,
                        fornecedor=fornecedor,
                        numero_fatura=numero,
                        data_emissao=data_emissao,
                        data_vencimento=data_venc,
                        valor=valor,
                        estado=estado
                    )
                    session.add(inv)
                    results["importadas"] += 1

            except Exception as e:
                results["erros"].append({"linha": int(i) + 2, "erro": str(e)})  # +2 por causa do header e index 0

        session.commit()

    return results


@app.get("/companies/{company_id}/invoices")
def list_invoices(
    company_id: int,
    status: Optional[str] = None,  # "overdue" | "due_soon" | "paid" | None
    days: int = 15,
):
    today = date.today()
    soon_limit = today + timedelta(days=days)

    with Session(engine) as session:
        # base
        q = select(Invoice).where(Invoice.company_id == company_id)

        if status == "paid":
            q = q.where(Invoice.estado == "PAGA")
        elif status == "overdue":
            q = q.where(Invoice.estado != "PAGA").where(Invoice.data_vencimento < today)
        elif status == "due_soon":
            q = q.where(Invoice.estado != "PAGA").where(Invoice.data_vencimento >= today).where(Invoice.data_vencimento <= soon_limit)

        invoices = session.exec(q.order_by(Invoice.data_vencimento)).all()

    # Enriquecer com "categoria" simples para UI
    out = []
    for inv in invoices:
        if inv.estado == "PAGA":
            cat = "PAGA"
        elif inv.data_vencimento < today:
            cat = "VENCIDA"
        elif inv.data_vencimento <= soon_limit:
            cat = "A_VENCER"
        else:
            cat = "FUTURA"
        out.append({**inv.model_dump(), "categoria": cat})

    return {"today": str(today), "days": days, "items": out}


@app.patch("/invoices/{invoice_id}/mark_paid")
def mark_invoice_paid(invoice_id: int):
    with Session(engine) as session:
        inv = session.get(Invoice, invoice_id)
        if not inv:
            raise HTTPException(404, "Fatura não encontrada")
        inv.estado = "PAGA"
        session.add(inv)
        session.commit()
        return {"ok": True, "invoice_id": invoice_id}
@app.get("/ui", response_class=HTMLResponse)
def ui():
    return """
<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Importar Faturas</title>
  <style>
    body{font-family:Arial, sans-serif; max-width:760px; margin:40px auto; padding:0 16px;}
    .card{border:1px solid #ddd; border-radius:12px; padding:16px; margin:12px 0;}
    label{display:block; margin:10px 0 6px;}
    input,select,button{padding:10px; font-size:14px;}
    input,select{width:100%;}
    button{cursor:pointer;}
    pre{background:#111; color:#0f0; padding:12px; border-radius:8px; overflow:auto;}
    small{color:#555;}
  </style>
</head>
<body>
  <h1>Importar Faturas</h1>
  <div class="card">
    <p><small>1) Escolhe a empresa (ID)  2) Seleciona o ficheiro (.xlsx ou .csv)  3) Importar</small></p>

    <label for="companyId">Company ID</label>
    <input id="companyId" type="number" value="3" min="1" />

    <label for="file">Ficheiro (CSV/XLSX)</label>
    <input id="file" type="file" accept=".csv,.xlsx,.xls" />

    <div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
      <button id="btnImport">Importar</button>
      <button id="btnList">Listar Empresas</button>
      <button id="btnInvoices">Ver Faturas da Empresa</button>
    </div>
  </div>

  <div class="card">
    <h3>Resultado</h3>
    <pre id="out">{}</pre>
  </div>

<script>
const out = document.getElementById("out");
function show(x){ out.textContent = typeof x === "string" ? x : JSON.stringify(x, null, 2); }

async function listCompanies(){
  const r = await fetch("/companies");
  const j = await r.json();
  show(j);
}

async function listInvoices(){
  const companyId = document.getElementById("companyId").value;
  const r = await fetch(`/companies/${companyId}/invoices`);
  const j = await r.json();
  show(j);
}

async function importFile(){
  const companyId = document.getElementById("companyId").value;
  const f = document.getElementById("file").files[0];
  if(!companyId) return show("Falta companyId");
  if(!f) return show("Seleciona um ficheiro primeiro (.xlsx ou .csv)");

  const fd = new FormData();
  fd.append("file", f);

  const r = await fetch(`/companies/${companyId}/invoices/import`, {
    method: "POST",
    body: fd
  });

  const text = await r.text();
  try { show(JSON.parse(text)); }
  catch(e){ show(text); }
}

document.getElementById("btnImport").onclick = importFile;
document.getElementById("btnList").onclick = listCompanies;
document.getElementById("btnInvoices").onclick = listInvoices;
</script>
</body>
</html>
"""
