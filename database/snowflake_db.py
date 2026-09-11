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


def companies_list(company_name, limit=20):

    q = (company_name or "").strip().upper()
    if not q:
        return []

    # escape LIKE wildcards so user input can't act as a pattern
    q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{q}%"

    limit = max(1, min(int(limit), 500))   # inlined, so keep it a sane int

    query = f"""
        SELECT name FROM (
            SELECT DISTINCT {COL_S_COMPANY} AS name
            FROM {TABLE_NAME}
            WHERE {ACTIVE_ROWS}
              AND {COL_S_COMPANY} IS NOT NULL
              AND UPPER({COL_S_COMPANY}) LIKE %(company)s ESCAPE '\\\\'

            UNION

            SELECT DISTINCT {COL_B_COMPANY} AS name
            FROM {TABLE_NAME}
            WHERE {ACTIVE_ROWS}
              AND {COL_B_COMPANY} IS NOT NULL
              AND UPPER({COL_B_COMPANY}) LIKE %(company)s ESCAPE '\\\\'
        )
        ORDER BY name
        LIMIT {limit}
    """

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, {"company": pattern})
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

    # q = "ALLENBERG"
    # result = companies_list(q, limit=10)
    # print(f"Companies matching '{q}':", result)

    # query = build_query("ALLENBERG COTTON CO")
    # print("QUERY:", query)
    query = f"SELECT {SELECTED_COLUMNS_STR} FROM {TABLE_NAME} LIMIT 100"
    df = run_query(query)
    #print(df.columns.tolist())
    # print("Row count:", df.shape)
    print(df.to_string(index=False))


































