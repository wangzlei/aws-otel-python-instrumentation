import json
import boto3
import os


dynamodb = boto3.resource('dynamodb')
table_name = 'HistoricalRecordDynamoDBTable'
table = dynamodb.Table(table_name)

def lambda_handler(event, context):

    query_params = event.get('queryStringParameters', {})

    record_id = query_params.get('recordId')
    owners = query_params.get('owners')
    pet_id = query_params.get('petid')

    if record_id is None:
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'Missing recordId'})
        }

    try:
        table.put_item(
            Item={
                'recordId': record_id,
                'value': 'Case Report ' + record_id + ': Acute Gastroenteritis in a 3-Year-Old Female Labrador Retriever'
            }
        )
        return {
            'statusCode': 200,
            'body': json.dumps({'message': 'Item added successfully', 'recordId': record_id})
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }