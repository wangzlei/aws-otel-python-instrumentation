import os
import requests
import time


def lambda_handler(event, context):
    url2 = os.getenv('API_URL_2')
    url3 = os.getenv('API_URL_3')

    print(url2)
    print(url3)

    if not url2 or not url3:
        return {
            'statusCode': 500,
            'body': 'environment variable is not set'
        }
    status_code = ''
    body = ''
    try:
        response = requests.get(url2)
        status_code = response.status_code
        body = response.text
        body += '\n'

        for i in range(3):
            response = requests.get(url3)
            time.sleep(5)
        body += response.text


    except requests.exceptions.RequestException as e:
        return {
            'statusCode': 500,
            'body': f'Error calling url: {str(e)}'
        }
    return {
        'statusCode': status_code,
        'body': body
    }
