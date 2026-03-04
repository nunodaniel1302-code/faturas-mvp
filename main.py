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
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Dashboard de Faturas</title>
  <style>
    body{font-family:Arial, sans-serif; max-width:1100px; margin:30px auto; padding:0 16px;}
    h1{margin:0 0 12px;}
    .row{display:flex; gap:12px; flex-wrap:wrap; align-items:end;}
    .card{border:1px solid #e5e5e5; border-radius:14px; padding:14px; margin:12px 0;}
    label{display:block; margin:8px 0 6px; font-size:13px; color:#333;}
    input,select,button{padding:10px; font-size:14px; border-radius:10px; border:1px solid #ccc;}
    button{cursor:pointer; background:#fff;}
    button.primary{border-color:#333;}
    button:disabled{opacity:.5; cursor:not-allowed;}
    .filters button{margin-right:8px; margin-bottom:8px;}
    table{width:100%; border-collapse:separate; border-spacing:0; overflow:hidden; border-radius:12px; border:1px solid #e5e5e5;}
    th,td{padding:10px; border-bottom:1px solid #eee; text-align:left; font-size:14px;}
    th{background:#fafafa; font-weight:600;}
    tr:last-child td{border-bottom:none;}
    .badge{display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px; border:1px solid #ddd;}
    .vencida{border-color:#ffb3b3;}
    .a_vencer{border-color:#ffe5a6;}
    .paga{border-color:#b6f2c2;}
    .muted{color:#666; font-size:13px;}
    .right{display:flex; gap:8px; justify-content:flex-end; align-items:center;}
    .mono{font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;}
    .small{font-size:12px;}
    .success{color:#0a7;}
    .error{color:#b00;}
    .wrap{white-space:nowrap;}
  </style>
</head>
<body>
  <h1>Dashboard de Faturas</h1>
  <div class="muted">Carrega Excel/CSV e gere faturas (vencidas / a vencer / pagas).</div>

  <div class="card">
    <div class="row">
      <div style="min-width:220px; flex:1;">
        <label>Company ID</label>
        <input id="companyId" type="number" min="1" value="1"/>
      </div>

      <div style="min-width:260px; flex:2;">
        <label>Importar ficheiro (CSV/XLSX)</label>
        <input id="file" type="file" accept=".csv,.xlsx,.xls"/>
      </div>

      <div class="right">
        <button class="primary" id="btnImport">Importar</button>
        <button id="btnCompanies">Listar Empresas</button>
      </div>
    </div>

    <div id="msg" class="small muted" style="margin-top:10px;"></div>
  </div>

  <div class="card">
    <div class="filters">
      <button class="primary" data-filter="all">Todas</button>
      <button data-filter="overdue">Vencidas</button>
      <button data-filter="due_soon">A vencer (15 dias)</button>
      <button data-filter="paid">Pagas</button>
    </div>

    <div class="row" style="margin-top:10px;">
      <div style="min-width:220px;">
        <label>Dias (para “A vencer”)</label>
        <input id="days" type="number" min="1" value="15"/>
      </div>

      <div style="min-width:260px; flex:1;">
        <label>Pesquisa (fornecedor / nº fatura)</label>
        <input id="q" placeholder="ex.: EDP ou FT-001"/>
      </div>

      <div class="right">
        <button id="btnRefresh">Atualizar lista</button>
      </div>
    </div>

    <div style="margin-top:12px; overflow:auto;">
      <table>
        <thead>
          <tr>
            <th>Estado</th>
            <th>Fornecedor</th>
            <th>Nº Fatura</th>
            <th>Emissão</th>
            <th>Vencimento</th>
            <th class="wrap">Valor</th>
            <th>Ações</th>
          </tr>
        </thead>
        <tbody id="tbody">
          <tr><td colspan="7" class="muted">Sem dados ainda.</td></tr>
        </tbody>
      </table>
    </div>

    <div class="muted small" style="margin-top:10px;">
      Dica: “Importadas=0 / Atualizadas>0” significa que já existiam e foram atualizadas (sem duplicar).
    </div>
  </div>

  <div class="card">
    <div class="muted">Saída técnica (debug)</div>
    <pre id="out" class="mono small" style="background:#111; color:#0f0; padding:12px; border-radius:12px; overflow:auto;">{}</pre>
  </div>

<script>
const tbody = document.getElementById("tbody");
const out = document.getElementById("out");
const msg = document.getElementById("msg");
const companyIdEl = document.getElementById("companyId");
const daysEl = document.getElementById("days");
const qEl = document.getElementById("q");

let currentFilter = "all";
let lastItems = [];

function showOut(x){
  out.textContent = typeof x === "string" ? x : JSON.stringify(x, null, 2);
}
function setMsg(text, kind="muted"){
  msg.className = "small " + kind;
  msg.textContent = text || "";
}

function badge(cat){
  if(cat === "VENCIDA") return '<span class="badge vencida">🔴 Vencida</span>';
  if(cat === "A_VENCER") return '<span class="badge a_vencer">🟡 A vencer</span>';
  if(cat === "PAGA") return '<span class="badge paga">🟢 Paga</span>';
  return '<span class="badge">Futura</span>';
}

function render(items){
  lastItems = items || [];
  const q = (qEl.value || "").trim().toLowerCase();

  const filtered = lastItems.filter(it => {
    if(!q) return true;
    const a = (it.fornecedor || "").toLowerCase();
    const b = (it.numero_fatura || "").toLowerCase();
    return a.includes(q) || b.includes(q);
  });

  if(filtered.length === 0){
    tbody.innerHTML = '<tr><td colspan="7" class="muted">Sem resultados.</td></tr>';
    return;
  }

  tbody.innerHTML = filtered.map(it => {
    const canPay = it.estado !== "PAGA";
    return `
      <tr>
        <td>${badge(it.categoria)}</td>
        <td>${it.fornecedor}</td>
        <td class="mono">${it.numero_fatura}</td>
        <td class="wrap">${it.data_emissao}</td>
        <td class="wrap">${it.data_vencimento}</td>
        <td class="wrap">${Number(it.valor).toFixed(2)}</td>
        <td class="wrap">
          <button ${canPay ? "" : "disabled"} data-pay="${it.id}">
            Marcar paga
          </button>
        </td>
      </tr>
    `;
  }).join("");

  // bind actions
  document.querySelectorAll("button[data-pay]").forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute("data-pay");
      await markPaid(id);
    };
  });
}

async function fetchInvoices(){
  const companyId = companyIdEl.value;
  const days = Number(daysEl.value || 15);

  let url = `/companies/${companyId}/invoices`;
  if(currentFilter === "overdue") url += `?status=overdue&days=${days}`;
  if(currentFilter === "due_soon") url += `?status=due_soon&days=${days}`;
  if(currentFilter === "paid") url += `?status=paid&days=${days}`;

  setMsg("A carregar faturas...", "muted");
  const r = await fetch(url);
  const text = await r.text();
  try{
    const j = JSON.parse(text);
    showOut(j);
    render(j.items || []);
    setMsg(`OK — ${ (j.items||[]).length } faturas carregadas.`, "success");
  }catch(e){
    showOut(text);
    setMsg("Erro ao carregar faturas (ver saída técnica).", "error");
  }
}

async function listCompanies(){
  setMsg("A carregar empresas...", "muted");
  const r = await fetch("/companies");
  const text = await r.text();
  try{
    const j = JSON.parse(text);
    showOut(j);
    if(Array.isArray(j) && j.length){
      setMsg(`Empresas encontradas: ${j.length}. Ex.: ID ${j[0].id} = ${j[0].nome}`, "success");
    }else{
      setMsg("Não há empresas. Cria uma em /docs (POST /companies).", "error");
    }
  }catch(e){
    showOut(text);
    setMsg("Erro ao listar empresas.", "error");
  }
}

async function importFile(){
  const companyId = companyIdEl.value;
  const f = document.getElementById("file").files[0];
  if(!companyId) return setMsg("Falta Company ID.", "error");
  if(!f) return setMsg("Seleciona um ficheiro primeiro (.xlsx ou .csv).", "error");

  setMsg("A importar ficheiro...", "muted");
  const fd = new FormData();
  fd.append("file", f);

  const r = await fetch(`/companies/${companyId}/invoices/import`, { method:"POST", body: fd });
  const text = await r.text();

  try{
    const j = JSON.parse(text);
    showOut(j);
    if(j.detail){
      setMsg(j.detail, "error");
      return;
    }
    setMsg(`Import OK — lidas ${j.lidas}, importadas ${j.importadas}, atualizadas ${j.atualizadas}.`, "success");
    await fetchInvoices();
  }catch(e){
    showOut(text);
    setMsg("Erro no import (ver saída técnica).", "error");
  }
}

async function markPaid(invoiceId){
  setMsg("A marcar como paga...", "muted");
  const r = await fetch(`/invoices/${invoiceId}/mark_paid`, { method:"PATCH" });
  const text = await r.text();
  try{
    const j = JSON.parse(text);
    showOut(j);
    setMsg("Marcada como paga ✅", "success");
    await fetchInvoices();
  }catch(e){
    showOut(text);
    setMsg("Erro ao marcar como paga.", "error");
  }
}

// filters
document.querySelectorAll(".filters button").forEach(b => {
  b.onclick = async () => {
    document.querySelectorAll(".filters button").forEach(x => x.classList.remove("primary"));
    b.classList.add("primary");
    currentFilter = b.getAttribute("data-filter");
    await fetchInvoices();
  };
});

document.getElementById("btnImport").onclick = importFile;
document.getElementById("btnCompanies").onclick = listCompanies;
document.getElementById("btnRefresh").onclick = fetchInvoices;
qEl.oninput = () => render(lastItems);

// auto-load
setTimeout(fetchInvoices, 200);
</script>
</body>
</html>
"""
