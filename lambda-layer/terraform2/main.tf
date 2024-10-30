#### DynamoDB
resource "aws_dynamodb_table" "my_table" {
  name         = "HistoricalRecordDynamoDBTable"
  billing_mode = "PAY_PER_REQUEST"

  attribute {
    name = "recordId"
    type = "S"
  }

  hash_key = "recordId"
}

### IAM role
resource "aws_iam_role" "lambda_exec_role" {
  name = "lambda_exec_role3"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action    = "sts:AssumeRole",
      Effect    = "Allow",
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_policy" "lambda_policy" {
  name        = "lambda_policy3"
  description = "IAM policy for Lambda to write logs to CloudWatch"

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action   = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      Effect   = "Allow",
      Resource = "arn:aws:logs:*:*:*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "admin_policy" {
  role       = aws_iam_role.lambda_exec_role.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

resource "aws_iam_role_policy_attachment" "lambda_policy_attach" {
  role       = aws_iam_role.lambda_exec_role.name
  policy_arn = aws_iam_policy.lambda_policy.arn
}

### lambda layer
resource "aws_lambda_layer_version" "sdk_layer" {
  layer_name          = "AppSignalsPythonLayer"
  filename            = "${path.module}/../src/build/aws-opentelemetry-python-layer.zip"
  compatible_runtimes = ["python3.10", "python3.11", "python3.12"]
  license_info        = "Apache-2.0"
  source_code_hash    = filebase64sha256("${path.module}/../src/build/aws-opentelemetry-python-layer.zip")
}

###### lambda functions
resource "aws_lambda_function" "my_lambda" {
  function_name = "appointment-service-create"

  handler = "lambda_function.lambda_handler"
  runtime = "python3.12"
  timeout = 30

  role = aws_iam_role.lambda_exec_role.arn

  filename         = "${path.module}/../sample-apps2/build/function.zip"
  tracing_config {
    mode = "Active"
  }
  layers = compact([aws_lambda_layer_version.sdk_layer.arn])

  environment {
    variables = {
      AWS_LAMBDA_EXEC_WRAPPER = "/opt/otel-instrument",
      OTEL_TRACES_EXPORTER = "console,otlp"
    }
  }
}

##### Lambda 2
resource "aws_lambda_function" "my_lambda2" {
  function_name = "appointment-service-list"

  handler = "lambda_function.lambda_handler"
  runtime = "python3.12"
  timeout = 30

  role = aws_iam_role.lambda_exec_role.arn

  filename         = "${path.module}/../sample-apps2/build2/function.zip"
  tracing_config {
    mode = "Active"
  }
  layers = compact([aws_lambda_layer_version.sdk_layer.arn])

  environment {
    variables = {
      AWS_LAMBDA_EXEC_WRAPPER = "/opt/otel-instrument",
      OTEL_TRACES_EXPORTER = "console,otlp"
    }
  }
}

##### Lambda 3
resource "aws_lambda_function" "my_lambda3" {
  function_name = "appointment-service-get"

  handler = "lambda_function.lambda_handler"
  runtime = "python3.12"
  timeout = 30

  role = aws_iam_role.lambda_exec_role.arn

  filename         = "${path.module}/../sample-apps2/build3/function.zip"
  tracing_config {
    mode = "Active"
  }
  layers = compact([aws_lambda_layer_version.sdk_layer.arn])

  environment {
    variables = {
      AWS_LAMBDA_EXEC_WRAPPER = "/opt/otel-instrument",
      OTEL_TRACES_EXPORTER = "console,otlp"
    }
  }

  publish       = true
}

resource "aws_lambda_alias" "my_lambda_alias3" {
  name             = "prod"
  function_name    = aws_lambda_function.my_lambda3.function_name
  function_version = aws_lambda_function.my_lambda3.version
}


####### API GW
resource "aws_api_gateway_rest_api" "api" {
  name        = "appointment-service-gateway"
  description = "API Gateway for Lambda function"
}

### path 1
resource "aws_api_gateway_resource" "resource" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "add"
}

resource "aws_api_gateway_method" "method" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.resource.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "integration" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  resource_id = aws_api_gateway_resource.resource.id
  http_method = aws_api_gateway_method.method.http_method

  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = aws_lambda_function.my_lambda.invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.my_lambda.function_name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

### path 2
resource "aws_api_gateway_resource" "resource2" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "list"
}

resource "aws_api_gateway_method" "method2" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.resource2.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "integration2" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.resource2.id
  http_method             = aws_api_gateway_method.method2.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.my_lambda2.invoke_arn
}

resource "aws_lambda_permission" "api_gateway2" {
  statement_id  = "AllowAPIGatewayInvoke2"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.my_lambda2.function_name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

### path 3
resource "aws_api_gateway_resource" "resource3" {
  rest_api_id = aws_api_gateway_rest_api.api.id
  parent_id   = aws_api_gateway_rest_api.api.root_resource_id
  path_part   = "get"
}

resource "aws_api_gateway_method" "method3" {
  rest_api_id   = aws_api_gateway_rest_api.api.id
  resource_id   = aws_api_gateway_resource.resource3.id
  http_method   = "GET"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "integration3" {
  rest_api_id             = aws_api_gateway_rest_api.api.id
  resource_id             = aws_api_gateway_resource.resource3.id
  http_method             = aws_api_gateway_method.method3.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_alias.my_lambda_alias3.invoke_arn
}

resource "aws_lambda_permission" "api_gateway3" {
  statement_id  = "AllowAPIGatewayInvoke3"
  action        = "lambda:InvokeFunction"
#  function_name = aws_lambda_function.my_lambda3.function_name
  function_name = aws_lambda_alias.my_lambda_alias3.function_name
  qualifier = aws_lambda_alias.my_lambda_alias3.name
  principal     = "apigateway.amazonaws.com"

  source_arn = "${aws_api_gateway_rest_api.api.execution_arn}/*/*"
}

# deploy
resource "aws_api_gateway_deployment" "deployment" {
  depends_on = [
    aws_api_gateway_integration.integration,
    aws_api_gateway_integration.integration2,
    aws_api_gateway_integration.integration3,
  ]

  rest_api_id = aws_api_gateway_rest_api.api.id
}

resource "aws_api_gateway_stage" "prod" {
  stage_name           = "prod"
  rest_api_id          = aws_api_gateway_rest_api.api.id
  deployment_id        = aws_api_gateway_deployment.deployment.id
  xray_tracing_enabled = true

  depends_on = [
    aws_api_gateway_deployment.deployment
  ]
}

output "api_add_record" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/add?owners=lw&petid=dog&recordId=1"
}

output "api_list_record" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/list?owners=lw&petid=dog"
}

output "api_query_record" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/get?owners=lw&petid=dog&recordId=1"
}