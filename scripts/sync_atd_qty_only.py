import os
import ftplib
import csv
import re
import logging
import requests
import time
from dotenv import load_dotenv
from collections import defaultdict
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
FEED_DIR = os.path.join(BASE_DIR, 'feeds')
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(FEED_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f'atd_qty_sync_{datetime.date.today()}.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
)

load_dotenv(os.path.join(BASE_DIR, '.env'))

# ATD FTP (source)
FTP_HOST = os.environ.get('ATD_FTP_HOST')
FTP_USER = os.environ.get('ATD_FTP_USER')
FTP_PASS = os.environ.get('ATD_FTP_PASS')
FTP_DIR  = os.environ.get('ATD_FTP_DIR', '/uploads/wheels_below_retail/')

# WBR FTP (destination)
WBR_FTP_HOST = os.environ.get('FTP_HOST')
WBR_FTP_USER = os.environ.get('FTP_USER')
WBR_FTP_PASS = os.environ.get('FTP_PASS')

# Shopify
SHOPIFY_STORE_URL    = os.environ.get('SHOPIFY_STORE_URL')
SHOPIFY_ACCESS_TOKEN = os.environ.get('SHOPIFY_ACCESS_TOKEN')
SHOPIFY_LOCATION_ID  = os.environ.get('SHOPIFY_LOCATION_ID', 'gid://shopify/Location/91638464747')

WHEEL_BRANDS = {
    'Advanti Racing', 'Boyd Coddington', 'Cragar', 'Dropstars', 'Dropstars Trail Series',
    'Edge Off Road', 'Edge Street', 'Fittipaldi', 'Focal', 'Gear Off Road', 'Konig',
    'Mamba', 'Maxxim', 'Mickey Thompson', 'Motiv', 'Motiv Offroad', 'OE Performance',
    'OEP', 'Pacer', 'Platinum', 'TIS', 'TIS Motorsports', 'Ultra', 'Raceline', 'Raceline Forged'
}

CSV_FIELDS = ['Brand', 'Model', 'Pn', 'MFG', 'Price', 'MAP', 'Inventory']
PRICE_FILE = 'pricefile_for_location_573314.csv'


def clean_val(val):
    if not val:
        return ''
    val = val.strip().replace('$', '').replace(',', '')
    m = re.match(r'="(.+)"', val)
    return m.group(1) if m else val


def parse_price_file(local_path):
    prices = {}
    if not os.path.exists(local_path):
        return prices
    with open(local_path, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            sku = clean_val(row.get(' Supplier No', ''))
            price = clean_val(row.get(' Price', ''))
            map_p = clean_val(row.get(' MAP', ''))
            if sku:
                prices[sku] = {
                    'price': f"{float(price):.2f}" if price else '0.00',
                    'map':   f"{float(map_p):.2f}" if map_p else '0.00',
                }
    return prices


def get_latest_inventory_filename():
    try:
        ftp = ftplib.FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(FTP_DIR)
        files = []
        ftp.retrlines('NLST', files.append)
        inv_files = [f for f in files if f.startswith('384408-665056-T1-inventory-') and f.endswith('.csv')]
        inv_files.sort()
        ftp.quit()
        return inv_files[-1] if inv_files else None
    except Exception as e:
        logging.error(f"ATD FTP error: {e}")
        return None


def download_ftp_file(remote_name, local_name):
    try:
        ftp = ftplib.FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(FTP_DIR)
        with open(local_name, 'wb') as f:
            ftp.retrbinary(f'RETR {remote_name}', f.write)
        ftp.quit()
        return True
    except Exception as e:
        logging.error(f"ATD FTP download error ({remote_name}): {e}")
        return False


def upload_to_wbr(local_file, remote_name):
    try:
        logging.info(f"Uploading {remote_name} to WBR FTP...")
        ftp = ftplib.FTP(WBR_FTP_HOST, timeout=60)
        ftp.login(WBR_FTP_USER, WBR_FTP_PASS)
        with open(local_file, 'rb') as f:
            ftp.storbinary(f'STOR {remote_name}', f)
        ftp.quit()
        logging.info(f"{remote_name} uploaded OK")
    except Exception as e:
        logging.error(f"WBR FTP upload error ({remote_name}): {e}")


def shopify_graphql_request(query, variables=None):
    url = f"https://{SHOPIFY_STORE_URL}/admin/api/2024-01/graphql.json"
    headers = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN, 'Content-Type': 'application/json'}
    while True:
        try:
            r = requests.post(url, headers=headers, json={'query': query, 'variables': variables})
            data = r.json()
            if 'errors' in data:
                for error in data['errors']:
                    if error.get('extensions', {}).get('code') == 'THROTTLED':
                        time.sleep(5)
                        break
                else:
                    return data
            else:
                cost = data.get('extensions', {}).get('cost', {})
                if cost.get('throttleStatus', {}).get('currentlyAvailable', 1000) < 500:
                    time.sleep(2)
                return data
        except Exception as e:
            logging.error(f"Shopify request error: {e}, retrying...")
            time.sleep(5)


def sync_shopify(inventory_qty):
    # Fetch all Shopify variants
    logging.info("Fetching Shopify variants...")
    shopify_data = {}
    has_next_page = True
    cursor = None
    while has_next_page:
        res = shopify_graphql_request("""
            query getVariants($cursor: String) {
              productVariants(first: 250, after: $cursor) {
                pageInfo { hasNextPage endCursor }
                edges { node { id sku inventoryItem { id } } }
              }
            }
        """, {'cursor': cursor})
        if not res or 'data' not in res:
            break
        for edge in res['data']['productVariants']['edges']:
            node = edge['node']
            if node['sku']:
                shopify_data[node['sku']] = node
        page_info = res['data']['productVariants']['pageInfo']
        has_next_page = page_info.get('hasNextPage', False)
        cursor = page_info.get('endCursor')
    logging.info(f"Found {len(shopify_data):,} Shopify variants")

    # Build update list for matched SKUs
    items_to_set = [
        {'inventoryItemId': node['inventoryItem']['id'], 'locationId': SHOPIFY_LOCATION_ID, 'quantity': inventory_qty[sku]}
        for sku, node in shopify_data.items() if sku in inventory_qty
    ]
    logging.info(f"Matched {len(items_to_set):,} ATD SKUs to update")

    # Find stale items at ATD location (available > 0 but no longer in ATD feed)
    logging.info("Checking for stale quantities at ATD location...")
    stale_count = 0
    has_next_page = True
    cursor = None
    while has_next_page:
        res = shopify_graphql_request("""
            query getATDLevels($location: ID!, $cursor: String) {
              location(id: $location) {
                inventoryLevels(first: 250, after: $cursor) {
                  pageInfo { hasNextPage endCursor }
                  edges { node { available item { id sku } } }
                }
              }
            }
        """, {'location': SHOPIFY_LOCATION_ID, 'cursor': cursor})
        if not res or 'data' not in res:
            break
        location_data = res['data'].get('location') or {}
        if not location_data:
            logging.error(f"Location {SHOPIFY_LOCATION_ID} not found — check SHOPIFY_LOCATION_ID secret")
            break
        levels = location_data.get('inventoryLevels', {})
        for edge in levels.get('edges', []):
            node = edge['node']
            qty = node.get('available') or 0
            sku = node['item'].get('sku', '')
            item_id = node['item'].get('id')
            if qty > 0 and sku and sku not in inventory_qty:
                stale_count += 1
                logging.info(f"Stale: SKU {sku} available={qty}, zeroing")
                items_to_set.append({'inventoryItemId': item_id, 'locationId': SHOPIFY_LOCATION_ID, 'quantity': 0})
        page_info = levels.get('pageInfo', {})
        has_next_page = page_info.get('hasNextPage', False)
        cursor = page_info.get('endCursor')
    logging.info(f"Stale items to zero: {stale_count}")

    # Batch update all in one go (100 per mutation — same as USAF)
    if items_to_set:
        logging.info(f"Syncing {len(items_to_set):,} inventory updates...")
        for i in range(0, len(items_to_set), 100):
            batch = items_to_set[i:i+100]
            shopify_graphql_request("""
                mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
                  inventorySetOnHandQuantities(input: $input) { userErrors { message } }
                }
            """, {"input": {"reason": "correction", "setQuantities": batch}})
        logging.info("Shopify inventory sync complete")


def main():
    start_time = datetime.datetime.now()
    logging.info("=== ATD Sync Started ===")

    inv_remote = get_latest_inventory_filename()
    if not inv_remote:
        logging.error("No inventory file found on ATD FTP.")
        return
    logging.info(f"Latest file: {inv_remote}")

    inv_local   = os.path.join(FEED_DIR, 'latest_atd_inventory.csv')
    price_local = os.path.join(FEED_DIR, PRICE_FILE)

    if not download_ftp_file(inv_remote, inv_local):
        return
    download_ftp_file(PRICE_FILE, price_local)
    price_map = parse_price_file(price_local)
    logging.info(f"Price file: {len(price_map)} SKUs loaded")

    # Parse inventory
    inventory_qty = defaultdict(int)
    brand_rows = {}
    with open(inv_local, 'r', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            sku   = (row.get('ManufacturerPartNumber') or '').strip()
            brand = (row.get('Brand') or '').strip()
            qty   = int(row.get('TotalQOH') or '0')
            if sku:
                inventory_qty[sku] += qty
                brand_rows[(brand, sku)] = row

    # Build tires/wheels CSVs
    tires_rows, wheels_rows = [], []
    seen = set()
    for (brand, sku), row in brand_rows.items():
        if sku in seen:
            continue
        seen.add(sku)
        p = price_map.get(sku, {})
        entry = {
            'Brand':     brand,
            'Model':     (row.get('PartDescription') or '').strip(),
            'Pn':        sku,
            'MFG':       sku,
            'Price':     p.get('price', '0.00'),
            'MAP':       p.get('map', '0.00'),
            'Inventory': inventory_qty[sku],
        }
        if brand in WHEEL_BRANDS:
            wheels_rows.append(entry)
        else:
            tires_rows.append(entry)

    logging.info(f"Tires: {len(tires_rows)} | Wheels: {len(wheels_rows)}")

    tires_file  = os.path.join(FEED_DIR, 'ATD_Tires_Inventory.csv')
    wheels_file = os.path.join(FEED_DIR, 'ATD_Wheels_Inventory.csv')
    for path, rows in [(tires_file, tires_rows), (wheels_file, wheels_rows)]:
        with open(path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    # Upload CSVs to WBR FTP
    upload_to_wbr(tires_file,  'ATD_Tires_Inventory.csv')
    upload_to_wbr(wheels_file, 'ATD_Wheels_Inventory.csv')

    # Sync to Shopify
    sync_shopify(inventory_qty)

    logging.info(f"=== ATD Sync Done in {datetime.datetime.now() - start_time} ===")


if __name__ == "__main__":
    main()
