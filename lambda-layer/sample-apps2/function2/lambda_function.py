import json
import boto3
import os

dynamodb = boto3.resource('dynamodb')
table_name = 'HistoricalRecordDynamoDBTable'
table = dynamodb.Table(table_name)

def lambda_handler(event, context):
    query_params = event.get('queryStringParameters', {})

    owners = query_params.get('owners')
    pet_id = query_params.get('petid')

    try:
        response = table.scan()
        items = response.get('Items', [])

        print("Record IDs in DynamoDB Table:")
        for item in items:
            print(item['recordId'])

        record_ids = [record['recordId'] for record in items if 'recordId' in record]

        return {
            'statusCode': 200,
            # 'body': json.dumps({'message': 'Records retrieved successfully', 'items': items})
            'body': json.dumps({
                'recordIds': record_ids
            }),
            'headers': {
                'Content-Type': 'application/json'
            }
        }
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
