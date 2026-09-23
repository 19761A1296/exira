import os
import snowflake.connector
from dotenv import load_dotenv
import pandas as pd
load_dotenv()

TABLE = os.getenv("TRADE_TABLE_NAME")


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




# --- change these to match your Actual column names ---
SELLER_COL = "S_COMPANY"
BUYER_COL = "B_COMPANY"
HSCODE_COL = "HS_CODE"
VALUE_COL = "SHIPMENT_VALUE"
PROD_ATTR = "PROD_ATTR"
PRODUCT_DESCRIPTION_NORM = "PRODUCT_DESCRIPTION_NORM"


def top_hscodes(company, role, limit=3):
    """role is 'seller' (exports) or 'buyer' (imports)."""
    col = SELLER_COL if role == "seller" else BUYER_COL

    query = f"""
        SELECT {HSCODE_COL} AS "hscode",
               LISTAGG(DISTINCT {PROD_ATTR}, ', ') AS "prod_attr",
               SUM({VALUE_COL}) AS "total_value",
               COUNT(*) AS "shipments"
        FROM {TABLE}
        WHERE UPPER(TRIM({col})) = %s
        GROUP BY {HSCODE_COL}
        ORDER BY "total_value" DESC
        LIMIT {limit}
    """

    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(query, [company.strip().upper()])
        df = cur.fetch_pandas_all()
    finally:
        conn.close()

    df["prod_attr"] = df["prod_attr"].apply(lambda v: ", ".join(sorted({a.strip().upper() for a in str(v).split(",") if a.strip()})))
    return df

def bom_data(company):
    exports = top_hscodes(company, "seller")
    imports = top_hscodes(company, "buyer")

    print(f"\n=== {company} ===")

    print("Length of imports and expots ...",imports.shape,exports.shape)
    print("\nTop exported HS codes:")
    if exports.empty:
        print("  not found as a seller")
    else:
        #print(exports.to_string(index=False))
        pass
        
        

    print("\nTop imported HS codes:")
    if imports.empty:
        print("  not found as a buyer")
    else:
        #print(imports.to_string(index=False))
        pass
        


    if exports.empty and imports.empty:
        print("\nNo records found")
        return None, None

    if imports.empty:
        role, best = "EXPORT", exports.iloc[0]
    elif exports.empty:
        role, best = "IMPORT", imports.iloc[0]
    elif exports.iloc[0]["total_value"] >= imports.iloc[0]["total_value"]:
        role, best = "EXPORT", exports.iloc[0]
    else:
        role, best = "IMPORT", imports.iloc[0]


    bomb_data = {
        "role": role,
        "hscode": best["hscode"],
        "prod_attr": best["prod_attr"],
        "prod_description": best["prod_description"],
        "total_value": best["total_value"],
        "shipments": best["shipments"],
    }

    print(f"\nStrongest side: {role}")
    print(f"  HS code   : {best['hscode']}")
    print(f"  Attributes: {best['prod_attr']}")
    print(f"  Value     : {best['total_value']:,}")
    print(f"  Shipments : {best['shipments']}")

    return bomb_data




if __name__ == "__main__":
   
    #search_company("KRESHNAA  PVT LTD")
    print(bom_data("STANDARD PRIME EXPORT INDORE PVT LTD"))