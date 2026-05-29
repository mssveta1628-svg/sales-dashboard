"""
Sales Dashboard — утренний отчёт отдела продаж (Wildberries).
"""

import calendar
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml

from db import db_available, load_from_db, load_from_excel

# ── Конфиг страницы ──────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Sales Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Загрузка конфига менеджеров ───────────────────────────────────────────────
@st.cache_data
def load_managers():
    with open("managers_config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["managers"]


managers = load_managers()
manager_by_name = {m["name"]: m for m in managers}

# ── Авторизация ───────────────────────────────────────────────────────────────
def login_page():
    st.title("📊 Sales Dashboard")
    st.subheader("Вход")

    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        name = st.selectbox("Выберите имя", [""] + [m["name"] for m in managers])
        pin = st.text_input("PIN-код", type="password", max_chars=10)
        if st.button("Войти", use_container_width=True, type="primary"):
            if name and name in manager_by_name:
                expected = str(manager_by_name[name].get("pin", ""))
                if pin == expected:
                    st.session_state["manager"] = name
                    st.rerun()
                else:
                    st.error("Неверный PIN")
            else:
                st.error("Выберите имя")


if "manager" not in st.session_state:
    login_page()
    st.stop()

# ── Данные менеджера ──────────────────────────────────────────────────────────
manager = manager_by_name[st.session_state["manager"]]
articles = [str(a) for a in manager["articles"]]
art_set = set(articles)

today = date.today()
yesterday = today - timedelta(days=1)
days_elapsed = max(today.day - 1, 1)
days_in_month = calendar.monthrange(today.year, today.month)[1]

# ── Загрузка данных ───────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner="Загрузка данных…")
def get_data(arts_tuple, use_db: bool):
    if use_db:
        return load_from_db(arts_tuple)
    else:
        return load_from_excel()


use_db = db_available()
raw = get_data(tuple(articles), use_db)

orders_df = raw.get("orders", pd.DataFrame())
plan_df = raw.get("plan", pd.DataFrame())
adv_df = raw.get("advertising", pd.DataFrame())
stock_df = raw.get("stock", pd.DataFrame())

# Фильтрация по артикулам менеджера
if not orders_df.empty:
    orders_df = orders_df[orders_df["article"].isin(art_set)].copy()
if not plan_df.empty:
    plan_df = plan_df[plan_df["article"].isin(art_set)].copy()
if not adv_df.empty:
    adv_df = adv_df[adv_df["article"].isin(art_set)].copy()
if not stock_df.empty:
    stock_df = stock_df[stock_df["article"].isin(art_set)].copy()

# ── Сайдбар ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(f"### 👤 {manager['name']}")
    st.caption(f"Дата: {today.strftime('%d.%m.%Y')}")
    st.caption(f"Артикулов: {len(articles)}")
    st.caption(f"Источник: {'База данных' if use_db else 'Excel файл'}")
    st.divider()

    if st.button("🔄 Обновить данные"):
        st.cache_data.clear()
        st.rerun()

    if st.button("🚪 Выйти"):
        del st.session_state["manager"]
        st.rerun()

# ── Заголовок ─────────────────────────────────────────────────────────────────
st.title("📊 Утренний отчёт — Wildberries")
st.caption(f"Данные за {yesterday.strftime('%d.%m.%Y')}")

# ── Вычисление метрик ─────────────────────────────────────────────────────────

def compute_trend(plan_df: pd.DataFrame) -> pd.DataFrame:
    if plan_df.empty:
        return pd.DataFrame()
    df = plan_df.copy()
    df["daily_orders"] = df["actual_qty"] / days_elapsed
    df["projected_orders"] = df["daily_orders"] * days_in_month
    df["trend_orders_pct"] = (df["projected_orders"] / df["plan_qty"].replace(0, float("nan")) * 100).round(1)
    df["daily_revenue"] = df.get("actual_revenue", 0) / days_elapsed
    df["projected_revenue"] = df["daily_revenue"] * days_in_month
    df["trend_revenue_pct"] = (df["projected_revenue"] / df.get("plan_revenue", pd.Series([0] * len(df))).replace(0, float("nan")) * 100).round(1)
    return df


def compute_orders_drop(orders_df: pd.DataFrame, threshold: float = 20.0) -> pd.DataFrame:
    if orders_df.empty:
        return pd.DataFrame()
    yd = orders_df[orders_df["date"] == yesterday].groupby("article")["orders_qty"].sum()
    db = orders_df[orders_df["date"] == yesterday - timedelta(days=1)].groupby("article")["orders_qty"].sum()
    result = []
    for art in art_set:
        yd_val = yd.get(art, 0)
        db_val = db.get(art, 0)
        if db_val > 0:
            drop = (db_val - yd_val) / db_val * 100
            if drop >= threshold:
                result.append({"Артикул": art, "Позавчера": int(db_val), "Вчера": int(yd_val), "Падение, %": round(drop, 1)})
    return pd.DataFrame(result).sort_values("Падение, %", ascending=False) if result else pd.DataFrame()


def compute_traffic_potential(stock_df: pd.DataFrame, orders_df: pd.DataFrame, adv_df: pd.DataFrame) -> pd.DataFrame:
    if stock_df.empty:
        return pd.DataFrame()

    # Среднедневные заказы за 7 дней
    orders_7d = pd.Series(dtype=float)
    if not orders_df.empty:
        cutoff = yesterday - timedelta(days=6)
        last7 = orders_df[(orders_df["date"] >= cutoff) & (orders_df["date"] <= yesterday)]
        orders_7d = last7.groupby("article")["orders_qty"].sum()

    # DRR вчера
    drr_map = {}
    if not adv_df.empty:
        yd_adv = adv_df[adv_df["date"] == yesterday] if "date" in adv_df.columns else adv_df
        drr_map = dict(zip(yd_adv["article"], yd_adv["drr"]))

    result = []
    for _, row in stock_df.iterrows():
        art = str(row["article"])
        stock_val = int(row["stock_qty"])
        daily = float(orders_7d.get(art, 0)) / 7
        turnover = round(stock_val / daily) if daily > 0 else 999
        drr = drr_map.get(art, 0)
        if turnover > 40 and drr < 6:
            result.append({"Артикул": art, "Остаток, шт": stock_val, "Оборачиваемость, дн": int(turnover), "ДРР, %": drr})
    return pd.DataFrame(result).sort_values("Оборачиваемость, дн", ascending=False) if result else pd.DataFrame()


trend_df = compute_trend(plan_df)
drop_df = compute_orders_drop(orders_df)
traffic_df = compute_traffic_potential(stock_df, orders_df, adv_df)

# ── KPI карточки ──────────────────────────────────────────────────────────────
c1, c2, c3, c4 = st.columns(4)
behind_count = int((trend_df["trend_orders_pct"] < 100).sum()) if not trend_df.empty else 0
drop_count = len(drop_df)
traffic_count = len(traffic_df)

with c1:
    st.metric("Артикулов за планом", behind_count, delta=None,
              delta_color="inverse", help="Тренд к концу месяца < 100%")
with c2:
    st.metric("Падение заказов", drop_count, help=">20% вчера vs позавчера")
with c3:
    st.metric("Потенциал трафика", traffic_count, help="Оборачиваемость >40 дн И ДРР <6%")
with c4:
    if not adv_df.empty:
        yd_adv = adv_df[adv_df["date"] == yesterday] if "date" in adv_df.columns else adv_df
        high_drr = int((yd_adv["drr"] >= 20).sum())
    else:
        high_drr = 0
    st.metric("Высокий ДРР (>20%)", high_drr)

st.divider()

# ── Вкладки ───────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs(
    ["📈 Тренд плана", "⬇️ Заказы", "📦 Потенциал трафика", "💸 Реклама"]
)

# ─── Вкладка 1: Тренд плана ───────────────────────────────────────────────────
with tab1:
    st.subheader(f"Тренд к концу {today.strftime('%B %Y')}")

    if trend_df.empty:
        st.info("Нет данных по плану.")
    else:
        # Фильтр
        filter_col, _ = st.columns([1, 3])
        with filter_col:
            show_filter = st.selectbox("Показать", ["Все артикулы", "Только отстающие (тренд < 100%)"],
                                       key="trend_filter")

        plot_df = trend_df.copy()
        if show_filter == "Только отстающие (тренд < 100%)":
            plot_df = plot_df[plot_df["trend_orders_pct"] < 100]

        if plot_df.empty:
            st.success("✅ Все артикулы идут по плану или выше!")
        else:
            plot_df = plot_df.sort_values("trend_orders_pct")

            # График тренда по заказам
            fig = go.Figure()
            colors = ["#e74c3c" if v < 100 else "#2ecc71" for v in plot_df["trend_orders_pct"]]
            fig.add_trace(go.Bar(
                x=plot_df["article"].astype(str),
                y=plot_df["trend_orders_pct"],
                marker_color=colors,
                name="Тренд заказов, %",
                text=plot_df["trend_orders_pct"].apply(lambda v: f"{v}%"),
                textposition="outside",
            ))
            fig.add_hline(y=100, line_dash="dash", line_color="gray", annotation_text="100%")
            fig.update_layout(
                title="Тренд заказов к концу месяца, %",
                xaxis_title="Артикул",
                yaxis_title="%",
                height=400,
                showlegend=False,
            )
            st.plotly_chart(fig, use_container_width=True)

            # Таблица
            display = plot_df[["article", "plan_qty", "actual_qty", "trend_orders_pct", "trend_revenue_pct"]].copy()
            display.columns = ["Артикул", "План, шт", "Факт, шт", "Тренд заказов, %", "Тренд продаж, %"]
            display["Артикул"] = display["Артикул"].astype(str)
            st.dataframe(
                display.style.background_gradient(subset=["Тренд заказов, %"], cmap="RdYlGn", vmin=50, vmax=150),
                use_container_width=True, hide_index=True,
            )

# ─── Вкладка 2: Заказы ───────────────────────────────────────────────────────
with tab2:
    col_left, col_right = st.columns([1, 1])

    with col_left:
        st.subheader("⬇️ Падение заказов (>20%)")
        if drop_df.empty:
            st.success("Нет падений >20%")
        else:
            fig_drop = px.bar(
                drop_df, x="Артикул", y="Падение, %",
                color="Падение, %", color_continuous_scale="Reds",
                text="Падение, %", title="Падение заказов вчера vs позавчера, %",
            )
            fig_drop.update_traces(texttemplate="%{text}%", textposition="outside")
            fig_drop.add_hline(y=20, line_dash="dash", line_color="orange")
            fig_drop.update_layout(height=350, showlegend=False)
            st.plotly_chart(fig_drop, use_container_width=True)
            st.dataframe(drop_df, use_container_width=True, hide_index=True)

    with col_right:
        st.subheader("📊 Динамика заказов (7 дней)")
        if orders_df.empty:
            st.info("Нет данных по заказам.")
        else:
            art_filter = st.multiselect(
                "Артикулы для графика",
                options=sorted(orders_df["article"].unique()),
                default=sorted(drop_df["Артикул"].tolist())[:5] if not drop_df.empty else [],
                key="orders_art_filter",
            )
            if art_filter:
                cutoff = yesterday - timedelta(days=6)
                chart_data = orders_df[
                    (orders_df["article"].isin(art_filter)) &
                    (orders_df["date"] >= cutoff)
                ].sort_values("date")
                fig_line = px.line(
                    chart_data, x="date", y="orders_qty", color="article",
                    title="Заказы по дням", markers=True,
                    labels={"date": "Дата", "orders_qty": "Заказы, шт", "article": "Артикул"},
                )
                fig_line.update_layout(height=350)
                st.plotly_chart(fig_line, use_container_width=True)
            else:
                st.info("Выберите артикулы выше")

# ─── Вкладка 3: Потенциал трафика ────────────────────────────────────────────
with tab3:
    st.subheader("📦 Потенциал увеличить трафик")
    st.caption("Оборачиваемость > 40 дней И ДРР < 6%")

    if traffic_df.empty:
        st.success("Нет артикулов для увеличения трафика.")
    else:
        col_chart, col_table = st.columns([1, 1])
        with col_chart:
            fig_t = px.scatter(
                traffic_df, x="Оборачиваемость, дн", y="ДРР, %",
                size="Остаток, шт", text="Артикул",
                color="Оборачиваемость, дн", color_continuous_scale="Blues",
                title="Оборачиваемость vs ДРР",
            )
            fig_t.add_vline(x=40, line_dash="dash", line_color="red", annotation_text="40 дн")
            fig_t.add_hline(y=6, line_dash="dash", line_color="orange", annotation_text="6%")
            fig_t.update_traces(textposition="top center")
            fig_t.update_layout(height=400)
            st.plotly_chart(fig_t, use_container_width=True)

        with col_table:
            st.dataframe(
                traffic_df.style.background_gradient(subset=["Оборачиваемость, дн"], cmap="Blues"),
                use_container_width=True, hide_index=True, height=400,
            )

# ─── Вкладка 4: Реклама ──────────────────────────────────────────────────────
with tab4:
    st.subheader("💸 ДРР по артикулам")

    if adv_df.empty:
        st.info("Нет данных по рекламе.")
    else:
        yd_adv = adv_df[adv_df["date"] == yesterday].copy() if "date" in adv_df.columns else adv_df.copy()
        if yd_adv.empty:
            st.info("Нет рекламных данных за вчера.")
        else:
            drr_threshold = st.slider("Порог ДРР для выделения, %", 5, 50, 20, key="drr_thresh")
            yd_adv = yd_adv.sort_values("drr", ascending=False)

            colors_drr = ["#e74c3c" if v >= drr_threshold else "#3498db" for v in yd_adv["drr"]]
            fig_drr = go.Figure(go.Bar(
                x=yd_adv["article"].astype(str),
                y=yd_adv["drr"],
                marker_color=colors_drr,
                text=yd_adv["drr"].apply(lambda v: f"{v}%"),
                textposition="outside",
            ))
            fig_drr.add_hline(y=drr_threshold, line_dash="dash", line_color="red",
                              annotation_text=f"Порог {drr_threshold}%")
            fig_drr.update_layout(title="ДРР вчера, %", xaxis_title="Артикул",
                                  yaxis_title="%", height=400, showlegend=False)
            st.plotly_chart(fig_drr, use_container_width=True)

            display_adv = yd_adv[["article", "drr", "budget_spent"]].copy()
            display_adv.columns = ["Артикул", "ДРР, %", "Расход, ₽"]
            display_adv["Артикул"] = display_adv["Артикул"].astype(str)
            display_adv["Расход, ₽"] = display_adv["Расход, ₽"].apply(lambda x: f"{int(x):,}".replace(",", " "))
            st.dataframe(
                display_adv.style.background_gradient(subset=["ДРР, %"], cmap="RdYlGn_r", vmin=0, vmax=30),
                use_container_width=True, hide_index=True,
            )
