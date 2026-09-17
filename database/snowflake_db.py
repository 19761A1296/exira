import os
import snowflake.connector
from dotenv import load_dotenv
import pandas as pd


load_dotenv()

TABLE_NAME = os.getenv("TRADE_TABLE_NAME")
SELECTED_COLUMNS = [
    "TTYPE",
    "SELLER_COMPANY","SELLER_ADDRESS","SELLER_CITY","SELLER_STATE","SELLER_COUNTRY","PORT_OF_ORIGIN","COUNTRY_OF_ORIGIN",
    "HS_CODE",
    "UNIT","QUANTITY",
    "BUYER_COMPANY","BUYER_ADDRESS","BUYER_CITY","BUYER_STATE","BUYER_COUNTRY","PORT_OF_DESTINATION","COUNTRY_OF_DESTINATION",
    "LAST_MODIFIED","D_FLAG",
    "S_COMPANY",
    "B_COMPANY",
    "PRODUCT_DESCRIPTION",
    "PRODUCT_DESCRIPTION_NORM",
    "PROD_ATTR"
]
SELECTED_COLUMNS_STR = ", ".join(SELECTED_COLUMNS)

COL_S_COMPANY = "S_COMPANY"
COL_B_COMPANY = "B_COMPANY"
COL_D_FLAG    = "D_FLAG"
ACTIVE_ROWS = f"({COL_D_FLAG} IS NULL OR {COL_D_FLAG} = 0)"



import os
import snowflake.connector
import re 

TABLE_NAME    = os.getenv("TRADE_TABLE_NAME")
COL_S_COMPANY = "S_COMPANY"
COL_B_COMPANY = "B_COMPANY"
COL_D_FLAG    = "D_FLAG"
ACTIVE_ROWS   = f"({COL_D_FLAG} IS NULL OR {COL_D_FLAG} = 0)"


def _get_conn():
    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC"),
        role=os.getenv("SNOWFLAKE_ROLE"),
    )


def companies_list(company_name, limit=500, country=None, hs2=None):
    
    q = (company_name or "").strip().upper()
    if not q:
        return []

    def esc(text):
        return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    params = {"company": f"%{esc(q)}%"}

    country = (country or "").strip().upper()
    hs2 = re.sub(r"\D", "", hs2 or "")[:2]

    seller_extra = ""
    buyer_extra = ""

    if country:
        params["country"] = f"%{esc(country)}%"
        seller_extra += " AND UPPER(TRIM(SELLER_COUNTRY)) LIKE %(country)s ESCAPE '\\\\'"
        buyer_extra  += " AND UPPER(TRIM(BUYER_COUNTRY))  LIKE %(country)s ESCAPE '\\\\'"

    if hs2:
        params["hs2"] = hs2
        hs_clause = " AND LEFT(TRIM(HS_CODE), 2) = %(hs2)s"
        seller_extra += hs_clause
        buyer_extra += hs_clause

    limit = max(1, min(int(limit), 1000))

    query = f"""
        SELECT name FROM (
            SELECT DISTINCT {COL_S_COMPANY} AS name
            FROM {TABLE_NAME}
            WHERE {ACTIVE_ROWS}
              AND {COL_S_COMPANY} IS NOT NULL
              AND UPPER({COL_S_COMPANY}) LIKE %(company)s ESCAPE '\\\\'
              {seller_extra}

            UNION

            SELECT DISTINCT {COL_B_COMPANY} AS name
            FROM {TABLE_NAME}
            WHERE {ACTIVE_ROWS}
              AND {COL_B_COMPANY} IS NOT NULL
              AND UPPER({COL_B_COMPANY}) LIKE %(company)s ESCAPE '\\\\'
              {buyer_extra}
        )
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

    sql = f"SELECT {SELECTED_COLUMNS_STR} FROM {TABLE_NAME} WHERE {ACTIVE_ROWS}"

    if company:
        sql += (
            f" AND ({COL_S_COMPANY} = '{company}'"
            f" OR {COL_B_COMPANY} = '{company}')"
        )

    return sql

def run_query(query):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query)

            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

        df = pd.DataFrame(rows, columns=columns)

        return df

    finally:
        conn.close()



if __name__ == "__main__":

    q = "AB"
    result = companies_list(q, limit=10)
    print(f"Companies matching '{q}':", result)

    # query = build_query("ALLENBERG COTTON CO")
    # print("QUERY:", query)
    # query = f"SELECT {SELECTED_COLUMNS_STR} FROM {TABLE_NAME} LIMIT 100"
    # df = run_query(query)
    # #print(df.columns.tolist())
    # # print("Row count:", df.shape)
    # print(df.to_string(index=False))


































