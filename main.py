from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw
import requests
import os
import psycopg2
from random import randint
from dotenv import load_dotenv
from datetime import datetime, timezone

import uvicorn
from pyngrok import ngrok
import threading

load_dotenv()

USER = os.getenv("user")
PASSWORD = os.getenv("password")
HOST = os.getenv("host")
PORT = os.getenv("port")
DBNAME = os.getenv("dbname")

app = FastAPI()

def init_connection():
    conn = psycopg2.connect(
        user=USER,
        password=PASSWORD,
        host=HOST,
        port=PORT,
        dbname=DBNAME
    )
    cur = conn.cursor()
    return conn, cur


def start_ngrok():
    public_url = ngrok.connect(8000)
    print(f"\n🚀 Public URL: {public_url}\n")

def start_uvicorn():
    uvicorn.run(app, host="0.0.0.0", port=8000)


@app.post("/countries/refresh")
def refresh_countries():
    conn,cur = init_connection()
    try:
        response = requests.get("https://restcountries.com/v2/all?fields=name,capital,region,population,flag,currencies")
        country_data = response.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail={ "error": "External data source unavailable", "details": "Could not fetch data from Rest Countries" })
    
    try:
        response = requests.get("https://open.er-api.com/v6/latest/USD")
        exchange_rate_data = response.json()
    except Exception as e:
        raise HTTPException(status_code=503, detail={ "error": "External data source unavailable", "details": "Could not fetch data from Exchange Rate API" })
    
    last_refreshed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for country in country_data:
        name = country.get("name")
        capital = country.get("capital")
        region = country.get("region")
        population = country.get("population")
        flag = country.get("flag")
        currencies = country.get("currencies")

        # --- Validation ---
        if not name:
            raise HTTPException(400, detail={"error": "Validation failed", "details": {"name": "is required"}})
        if population is None or population < 0:
            raise HTTPException(400, detail={"error": "Validation failed", "details": {"population": "must be non-negative"}})

        # --- Currency logic ---
        currency_code = None
        exchange_rate = None
        estimated_gdp = None

        if currencies and len(currencies) > 0:
            currency_code = currencies[0].get("code")

            if currency_code:
                exchange_rate = exchange_rate_data.get("rates", {}).get(currency_code)
                if exchange_rate:
                    estimated_gdp = (population * randint(1000, 2000)) / exchange_rate

        # --- Insert or update ---
        cur.execute("""
            INSERT INTO countries 
                (name, capital, region, population, currency_code, exchange_rate, estimated_gdp, flag_url, last_refreshed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE 
                SET capital = EXCLUDED.capital,
                    region = EXCLUDED.region,
                    population = EXCLUDED.population,
                    currency_code = EXCLUDED.currency_code,
                    exchange_rate = EXCLUDED.exchange_rate,
                    estimated_gdp = EXCLUDED.estimated_gdp,
                    flag_url = EXCLUDED.flag_url,
                    last_refreshed_at = EXCLUDED.last_refreshed_at;
        """, (name, capital, region, population, currency_code, exchange_rate, estimated_gdp, flag, last_refreshed_at))

    # 3️⃣ Commit once after all inserts
    conn.commit()

    # 4️⃣ Generate summary image
    cur.execute("SELECT COUNT(*) FROM countries;")
    total_countries = cur.fetchone()[0]

    img = Image.new('RGB', (800, 400), color='white')
    draw = ImageDraw.Draw(img)
    draw.text((50, 50), "Country Summary", fill='black')
    draw.text((50, 100), f"Total Number Of Countries: {total_countries}", fill='black')

    cur.execute("""
        SELECT name, estimated_gdp 
        FROM countries 
        WHERE estimated_gdp IS NOT NULL 
        ORDER BY estimated_gdp DESC LIMIT 5;
    """)
    top_gdp = cur.fetchall()

    draw.text((50, 140), "Top 5 countries by estimated GDP", fill='black')
    y_offset = 175
    for name, gdp in top_gdp:
        draw.text((50, y_offset), f"{name}: {round(gdp, 2)}", fill='black')
        y_offset += 25

    draw.text((50, 320), f"Last Refreshed At: {last_refreshed_at}", fill='black')

    if not os.path.exists("cache"):
        os.makedirs("cache")
    img.save("cache/summary.png")

    # 5️⃣ Clean up
    cur.close()
    conn.close()

@app.get("/countries/image")
def serve_country_image():
    image_path = "cache/summary.png"
    if not os.path.exists(image_path):
        raise HTTPException(404, {"error": "Summary image not found"})
    return FileResponse(image_path, media_type="image/png")

@app.get("/countries")
def get_countries_with_filtering(region: str = None, currency:str = None, sort:str = None):
    conn, cur = init_connection()
    if region or currency or sort:
        query = ["SELECT * FROM countries WHERE 1=1"]
        if region:
            query.append(f"AND region ILIKE '{region}'")
        if currency:
            query.append(f"AND currency_code ILIKE '{currency}'")
        if sort:
            if sort.lower() == "gdp_desc":
                query.append("ORDER BY estimated_gdp DESC")
            elif sort.lower() == "gdp_asc":
                query.append("ORDER BY estimated_gdp ASC")
        query = " ".join(query) + ";"
        cur.execute(query)
        results = cur.fetchall()
    else:
        cur.execute("SELECT * FROM countries")
        results = cur.fetchall()
    
    if not results:
        raise HTTPException(status_code=404, detail={"error": "Country not found"})
    data = []
    for result in results:
        data.append({
            "id":result[0],
            "name": result[1],
            "capital": result[2],
            "region": result[3],
            "population": result[4],
            "currency_code":result[5],
            "exchange_rate": result[6],
            "estimated_gdp": result[7],
            "flag_url": result[8],
            "last_refreshed_at": result[9]
        })

    cur.close()
    conn.close()
    return data

@app.get("/countries/{name}")
def get_country_by_name(name: str):
    conn, cur = init_connection()
    cur.execute("SELECT * FROM countries WHERE name ILIKE %s;", (name,))
    result = cur.fetchone()
    if not result:
        raise HTTPException(404, {"error": "Country not found"})
    data = {
        "id":result[0],
        "name": result[1],
        "capital": result[2],
        "region": result[3],
        "population": result[4],
        "currency_code":result[5],
        "exchange_rate": result[6],
        "estimated_gdp": result[7],
        "flag_url": result[8],
        "last_refreshed_at": result[9]
    }
    cur.close()
    conn.close()
    return data

@app.delete("/countries/{name}")
def delete_country_by_name(name: str):
    conn, cur = init_connection()
    cur.execute("SELECT * FROM countries WHERE name = %s;", (name,))
    result = cur.fetchone()
    if not result:
        raise HTTPException(404, {"error": "Country not found"})
    cur.execute("DELETE FROM countries WHERE name = %s;", (name,))
    conn.commit()
    cur.close()
    conn.close()
    return

@app.get("/status")
def get_status():
    conn, cur = init_connection()
    cur.execute("SELECT COUNT(*) FROM countries;")
    country_count = cur.fetchone()[0]
    cur.execute("SELECT MAX(last_refreshed_at) FROM countries;")
    last_refreshed_at = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {
        "total_countries": country_count,
        "last_refreshed_at": last_refreshed_at
    }

if __name__ == "__main__":
    threading.Thread(target=start_ngrok).start()
    start_uvicorn()
