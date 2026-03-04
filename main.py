from datetime import date, datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, UploadFile, File, HTTPException
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
