import requests
import json
import os
import dotenv

dotenv.load_dotenv()
api_token = os.getenv("BITQUERY_API_TOKEN")


url = "https://streaming.bitquery.io/eap"

payload = json.dumps(
    {
        "query": '{ Solana {  DEXTrades(    limitBy: {count: 1, by: Trade_Buy_Currency_MintAddress}    limit: {count: 100}    orderBy: {descending: Block_Time }    where: {Trade: {Buy: {Price: {gt: 0.0000005}, Currency: {MintAddress: {notIn: ["11111111111111111111111111111111"]}}}, Dex: {ProtocolName: {is: "pump"}}}, Transaction: {Result: {Success: true}}}  ) { Block { Time }   Trade { Buy {   Currency {     Name     Symbol     MintAddress     Decimals     Fungible     Uri   } } Sell {   Currency {     Name     Symbol     MintAddress     Decimals     Fungible     Uri   } }    }  } } }',
        "variables": "{}",
    }
)
headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {api_token}",
}

response = requests.request("POST", url, headers=headers, data=payload, timeout=10)

print(response.text)
