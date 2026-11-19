import os
import re

import pandas as pd
import pymysql
from dotenv import load_dotenv

load_dotenv()

TABLE_NAME         = os.getenv("VELODB_TRADE_TABLE_NAME", "").strip()
COMPANY_TABLE_NAME = os.getenv("VELODB_COMPANY_TABLE_NAME", "").strip()

COL_S_COMPANY = "SELLER_COMPANY"
COL_B_COMPANY = "BUYER_COMPANY"

SELECTED_COLUMNS = [
    "TTYPE",
    "SELLER_COMPANY","SELLER_ADDRESS","SELLER_CITY","SELLER_STATE","SELLER_COUNTRY","PORT_OF_ORIGIN","COUNTRY_OF_ORIGIN",
    "HS_CODE",
    "UNIT","QUANTITY",
    "BUYER_COMPANY","BUYER_ADDRESS","BUYER_CITY","BUYER_STATE","BUYER_COUNTRY","PORT_OF_DESTINATION","COUNTRY_OF_DESTINATION",
    "LAST_MODIFIED",
    "PRODUCT_DESCRIPTION",
    "PRODUCT_DESCRIPTION_NORM",
    "PROD_ATTR"
]
SELECTED_COLUMNS_STR = ", ".join(SELECTED_COLUMNS)


def _get_conn():
    # VeloDB (Apache Doris) speaks the MySQL protocol
    return pymysql.connect(
        host=os.environ["VELODB_HOST"].strip(),
        port=int(os.environ["VELODB_PORT"]),
        user=os.environ["VELODB_USER"].strip(),
        password=os.environ["VELODB_PASSWORD"],
        database=os.getenv("VELODB_DATABASE", "").strip() or None,
        charset="utf8mb4",
        connect_timeout=30,
    )


def companies_list(company_name, limit=500, country=None, hs2=None):
    # hs2 is kept for signature compatibility; the company master table has no HS data
    q = (company_name or "").strip().upper()
    if not q:
        return []

    def esc(text):
        # backslash is the default LIKE escape character in VeloDB/Doris
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    params = {"company": f"%{esc(q)}%"}

    country = (country or "").strip().upper()
    country_clause = ""
    if country:
        params["country"] = f"%{esc(country)}%"
        country_clause = " AND UPPER(TRIM(selected_country)) LIKE %(country)s"

    limit = max(1, min(int(limit), 1000))

    query = f"""
        SELECT DISTINCT TRIM(name) AS name
        FROM {COMPANY_TABLE_NAME}
        WHERE name IS NOT NULL
          AND TRIM(name) <> ''
          AND UPPER(name) LIKE %(company)s
          {country_clause}
        ORDER BY name
        LIMIT {limit}
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    return [r[0] for r in rows]

def build_query(company_name=""):

    company = (company_name or "").strip()

    sql = f"SELECT {SELECTED_COLUMNS_STR} FROM {TABLE_NAME}"

    if company:
        sql += (
            f" WHERE ({COL_S_COMPANY} = '{company}')"
            f" OR ({COL_B_COMPANY} = '{company}')"
        )

    return sql

def run_query(query):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query)

            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

        return pd.DataFrame(rows, columns=columns)
    finally:
        conn.close()



if __name__ == "__main__":

    # q = "AB"
    # result = companies_list(q, limit=10)
    # print(f"Companies matching '{q}':", result)

    query = build_query("SHAHI EXPORTS PVT LTD")
    print("QUERY:", query)
    # query = f"SELECT {SELECTED_COLUMNS_STR} FROM {TABLE_NAME} LIMIT 100"
    df = run_query(query)
    # #print(df.columns.tolist())
    print("Row count:", df.shape)
    # output_path = "C:\\Users\\TDB\\Desktop\\shahi_imports_exports.xlsx"
    # df.to_excel(output_path, index=False)
    # print(df.to_string(index=False))


































