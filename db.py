"""
Подключение к базам данных и SQL-запросы.
Работает в двух режимах:
  - DB mode:   psycopg2, credentials из st.secrets / переменных окружения
  - File mode: читает из Excel-файла (sales_data.xlsx)
"""

import os
from datetime import date, timedelta
from functools import lru_cache
from typing import Optional

import pandas as pd
import streamlit as st


# ---------------------------------------------------------------------------
# Подключение
# ---------------------------------------------------------------------------

def _texmod_dsn() -> dict:
    try:
        s = st.secrets["texmod"]
        return dict(host=s["host"], port=s.get("port", 5432),
                    dbname=s["dbname"], user=s["user"], password=s["password"])
    except Exception:
        return {}


def _dwh_dsn() -> dict:
    try:
        s = st.secrets["dwh"]
        return dict(host=s["host"], port=s.get("port", 5432),
                    dbname=s["dbname"], user=s["user"], password=s["password"])
    except Exception:
        return {}


def db_available() -> bool:
    dsn = _texmod_dsn()
    return bool(dsn.get("host"))


def _query_texmod(sql: str, params=None) -> pd.DataFrame:
    import psycopg2
    with psycopg2.connect(**_texmod_dsn()) as conn:
        return pd.read_sql(sql, conn, params=params)


def _query_dwh(sql: str, params=None) -> pd.DataFrame:
    import psycopg2
    with psycopg2.connect(**_dwh_dsn()) as conn:
        return pd.read_sql(sql, conn, params=params)


# ---------------------------------------------------------------------------
# Загрузка через Excel (fallback)
# ---------------------------------------------------------------------------

def _excel_path() -> str:
    paths = [
        os.path.join(os.path.dirname(__file__), "data", "sales_data.xlsx"),
        os.path.join(os.path.dirname(__file__), "..", "sales_morning_bot", "data", "sales_data.xlsx"),
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return ""


@st.cache_data(ttl=3600)
def load_from_excel() -> dict:
    path = _excel_path()
    if not path:
        return {}
    xls = pd.ExcelFile(path)
    today = date.today()

    def parse(sheet):
        if sheet not in xls.sheet_names:
            return pd.DataFrame()
        df = xls.parse(sheet)
        df.columns = [c.strip().lower() for c in df.columns]
        return df

    orders = parse("orders")
    if not orders.empty:
        orders["date"] = pd.to_datetime(orders["date"]).dt.date
        orders["orders_qty"] = pd.to_numeric(orders["orders_qty"], errors="coerce").fillna(0)
        orders["article"] = orders["article"].astype(str).str.strip()

    plan = parse("sales_plan")
    if not plan.empty:
        plan["article"] = plan["article"].astype(str).str.strip()
        plan["plan_qty"] = pd.to_numeric(plan["plan_qty"], errors="coerce").fillna(0)
        plan["actual_qty"] = pd.to_numeric(plan["actual_qty"], errors="coerce").fillna(0)
        plan["plan_revenue"] = pd.to_numeric(plan.get("plan_revenue", 0), errors="coerce").fillna(0)
        plan["actual_revenue"] = pd.to_numeric(plan.get("actual_revenue", 0), errors="coerce").fillna(0)
        plan["month"] = plan["month"].astype(str).str[:7]
        plan = plan[plan["month"] == today.strftime("%Y-%m")]

    adv = parse("advertising")
    if not adv.empty:
        adv["date"] = pd.to_datetime(adv["date"]).dt.date
        adv["drr"] = pd.to_numeric(adv["drr"], errors="coerce").fillna(0)
        adv["budget_spent"] = pd.to_numeric(adv["budget_spent"], errors="coerce").fillna(0)
        adv["article"] = adv["article"].astype(str).str.strip()

    stock = parse("stock")
    if not stock.empty:
        stock["stock_qty"] = pd.to_numeric(stock["stock_qty"], errors="coerce").fillna(0)
        stock["article"] = stock["article"].astype(str).str.strip()

    return {"orders": orders, "plan": plan, "advertising": adv, "stock": stock}


# ---------------------------------------------------------------------------
# Загрузка через БД
# ---------------------------------------------------------------------------

@st.cache_data(ttl=3600)
def load_from_db(articles: tuple) -> dict:
    arts_str = "','".join(articles)
    arts_list = list(articles)
    today = date.today()
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)

    # 1. nm_id маппинг
    nm_map = _query_texmod(f"""
        SELECT DISTINCT article::text as article, nmid::text as nmid
        FROM technical.full_nom_cl
        WHERE company = 'SMRZ'
          AND article::text IN ('{arts_str}')
    """)

    nmids = tuple(nm_map["nmid"].tolist()) if not nm_map.empty else ()
    if not nmids:
        return {}

    nmids_str = "','".join(nmids)

    # 2. Заказы за 30 дней
    orders = _query_dwh(f"""
        SELECT f.nmid::text as nmid, f.date, SUM(f.orderscount) as orders_qty
        FROM wb.wb_sales_funnel f
        WHERE f.nmid::text IN ('{nmids_str}')
          AND f.date >= '{today - timedelta(days=30)}'
          AND f.date <= '{yesterday}'
        GROUP BY f.nmid, f.date
    """)
    if not orders.empty:
        orders = orders.merge(nm_map, on="nmid").groupby(["article", "date"])["orders_qty"].sum().reset_index()
        orders["date"] = pd.to_datetime(orders["date"]).dt.date

    # 3. Остатки
    stock = _query_dwh(f"""
        SELECT s.nm_id::text as nmid, SUM(s.quantity) as stock_qty
        FROM wb.wb_api_stocks s
        WHERE s.nm_id::text IN ('{nmids_str}')
          AND s.created_at = (
              SELECT MAX(created_at) FROM wb.wb_api_stocks WHERE nm_id = s.nm_id
          )
        GROUP BY s.nm_id
    """)
    if not stock.empty:
        stock = stock.merge(nm_map, on="nmid").groupby("article")["stock_qty"].sum().reset_index()

    # 4. План и факт
    plan = _query_texmod(f"""
        SELECT article::text, SUM(qty) as plan_qty, SUM(revenue) as plan_revenue
        FROM technical.plan_mp_article
        WHERE mp = 'wildberries' AND month = '{month_start}'
          AND article::text IN ('{arts_str}')
        GROUP BY article
    """)

    actual = _query_dwh(f"""
        SELECT m.article, SUM(f.orderscount) as actual_qty, SUM(f.orderssumrub) as actual_revenue
        FROM wb.wb_sales_funnel f
        JOIN (VALUES {",".join(f"('{a}','{n}')" for a, n in zip(nm_map.article, nm_map.nmid))}) as m(article, nmid)
          ON m.nmid = f.nmid::text
        WHERE f.date >= '{month_start}' AND f.date <= '{yesterday}'
        GROUP BY m.article
    """)

    if not plan.empty and not actual.empty:
        plan = plan.merge(actual, on="article", how="left").fillna(0)
    elif not plan.empty:
        plan["actual_qty"] = 0
        plan["actual_revenue"] = 0

    # 5. Реклама
    adv = _query_texmod(f"""
        SELECT a.nm_id::text as nmid,
               ROUND(SUM(a.sum)::numeric / NULLIF(SUM(a.sum_price)::numeric, 0) * 100, 1) as drr,
               SUM(a.sum)::int as budget_spent
        FROM wber_advert.advert_fullstats a
        WHERE a.date = '{yesterday}'
          AND a.nm_id::text IN ('{nmids_str}')
        GROUP BY a.nm_id
    """)
    if not adv.empty:
        adv = adv.merge(nm_map, on="nmid").groupby("article").agg(
            drr=("drr", "mean"), budget_spent=("budget_spent", "sum")
        ).reset_index()
        adv["date"] = yesterday

    return {"orders": orders, "plan": plan, "advertising": adv, "stock": stock}
