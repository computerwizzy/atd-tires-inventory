import os
import ftplib
import csv
import re
import logging
import datetime
from collections import defaultdict

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

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, '.env'))
except ImportError:
    env_file = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    os.environ.setdefault(k.strip(), v.strip())

# ATD FTP (source)
FTP_HOST = os.environ.get('ATD_FTP_HOST')
FTP_USER = os.environ.get('ATD_FTP_USER')
FTP_PASS = os.environ.get('ATD_FTP_PASS')
FTP_DIR  = os.environ.get('ATD_FTP_DIR', '/uploads/wheels_below_retail/')

# WBR FTP (destination)
WBR_FTP_HOST = os.environ.get('FTP_HOST')
WBR_FTP_USER = os.environ.get('FTP_USER')
WBR_FTP_PASS = os.environ.get('FTP_PASS')

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
        for row in csv.DictReader(f, delimiter='|'):
            sku   = (row.get('ManufacturerPartNumber') or '').strip()
            brand = (row.get('BrandName') or '').strip()
            qty   = int(row.get('QuantityAvailable') or '0')
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
            'Model':     (row.get('ProductDescription') or '').strip(),
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

    upload_to_wbr(tires_file,  'ATD_Tires_Inventory.csv')
    upload_to_wbr(wheels_file, 'ATD_Wheels_Inventory.csv')

    # Sync to Tires and Engine Performance only (SHOPIFY_STORE_URL_2 et al.)
    sync_tep_shopify_store(inventory_qty)

    logging.info(f"=== ATD Sync Done in {datetime.datetime.now() - start_time} ===")


def sync_tep_shopify_store(inventory_qty):
    """Sync inventory to the Tires and Engine Performance Shopify store only.
    TEP was added today as the '_2' store credentials
    (SHOPIFY_STORE_URL_2 / SHOPIFY_ACCESS_TOKEN_2 / SHOPIFY_LOCATION_ID_2).
    The original unsuffixed SHOPIFY_STORE_URL / _ACCESS_TOKEN / _LOCATION_ID
    is a different, pre-existing store and is intentionally ignored here so
    only TEP gets inventory writes from this workflow.
    """
    url = os.environ.get('SHOPIFY_STORE_URL_2')
    token = os.environ.get('SHOPIFY_ACCESS_TOKEN_2')
    location = os.environ.get('SHOPIFY_LOCATION_ID_2')

    if not (url and token and location):
        logging.info("No TEP Shopify credentials found in environment, skipping Shopify sync.")
        return

    logging.info(f"Syncing inventory to Shopify store: {url}")
    sync_shopify_inventory(inventory_qty, url, token, location)


def sync_shopify_inventory(inventory_qty, store_url, access_token, location_id):
    store_domain = store_url.replace('https://', '').replace('http://', '').strip('/')
    gql_url = f"https://{store_domain}/admin/api/2024-01/graphql.json"
    headers = {
        'X-Shopify-Access-Token': access_token,
        'Content-Type': 'application/json'
    }

    logging.info(f"Starting Shopify Inventory Sync to {store_domain}...")

    def clean_sku(s):
        if not s: return ""
        return re.sub(r'[^A-Z0-9]', '', s.upper().strip())

    clean_atd_map = {}
    for raw_sku, qty in inventory_qty.items():
        c = clean_sku(raw_sku)
        if c:
            clean_atd_map[c] = qty

    has_next = True
    cursor = None
    updated_count = 0

    import json
    import urllib.request

    while has_next:
        after_clause = f', after: "{cursor}"' if cursor else ''
        q = f'''
        {{
          products(first: 250{after_clause}) {{
            edges {{
              node {{
                id
                title
                variants(first: 50) {{
                  edges {{
                    node {{
                      id
                      sku
                      inventoryQuantity
                      inventoryItem {{
                        id
                      }}
                    }}
                  }}
                }}
              }}
            }}
            pageInfo {{
              hasNextPage
              endCursor
            }}
          }}
        }}
        '''

        try:
            req = urllib.request.Request(gql_url, data=json.dumps({'query': q}).encode(), headers=headers, method='POST')
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                prods = data.get('data', {}).get('products', {})
                edges = prods.get('edges', [])
                has_next = prods.get('pageInfo', {}).get('hasNextPage', False)
                cursor = prods.get('pageInfo', {}).get('endCursor')

                set_quantities = []
                for e in edges:
                    p_node = e['node']
                    p_title = p_node['title']
                    for vnode in p_node['variants']['edges']:
                        v = vnode['node']
                        raw_sku = (v['sku'] or '').strip()
                        c_sku = clean_sku(raw_sku)
                        store_qty = int(v['inventoryQuantity'] or 0)
                        item_id = v['inventoryItem']['id']

                        atd_qty = clean_atd_map.get(c_sku)
                        if atd_qty is None and c_sku.endswith('S'): atd_qty = clean_atd_map.get(c_sku[:-1])
                        if atd_qty is None and c_sku.endswith('C'): atd_qty = clean_atd_map.get(c_sku[:-1])
                        if atd_qty is None: atd_qty = clean_atd_map.get(c_sku + 'S') or clean_atd_map.get(c_sku + 'C')

                        if atd_qty is None:
                            m_part = re.search(r'([0-9A-Z]{4,}-[0-9A-Z]{4,})', p_title)
                            if m_part:
                                alt_sku = clean_sku(m_part.group(1))
                                atd_qty = clean_atd_map.get(alt_sku) or clean_atd_map.get(alt_sku + 'S')

                        if atd_qty is not None and atd_qty != store_qty:
                            set_quantities.append({
                                'inventoryItemId': item_id,
                                'locationId': location_id,
                                'quantity': atd_qty
                            })

                for idx in range(0, len(set_quantities), 50):
                    batch = set_quantities[idx:idx+50]
                    mutation = '''
                    mutation inventorySetOnHandQuantities($input: InventorySetOnHandQuantitiesInput!) {
                      inventorySetOnHandQuantities(input: $input) {
                        userErrors {
                          field
                          message
                        }
                      }
                    }
                    '''
                    payload = {
                        'query': mutation,
                        'variables': {
                            'input': {
                                'reason': 'correction',
                                'setQuantities': batch
                            }
                        }
                    }
                    req_mut = urllib.request.Request(gql_url, data=json.dumps(payload).encode(), headers=headers, method='POST')
                    with urllib.request.urlopen(req_mut) as r_m:
                        res_m = json.loads(r_m.read().decode())
                        errs = res_m.get('data', {}).get('inventorySetOnHandQuantities', {}).get('userErrors', [])
                        if not errs:
                            updated_count += len(batch)
                        else:
                            logging.error(f"Shopify batch update error: {errs}")
        except Exception as e:
            logging.error(f"Shopify pagination error: {e}")
            break

    logging.info(f"Shopify Sync Complete: Updated {updated_count} variants on store.")


if __name__ == "__main__":
    main()

