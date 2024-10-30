##### IAM role
resource "aws_iam_role" "lambda_exec_role1" {
  name = "lambda_exec_role4"

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

resource "aws_iam_role_policy_attachment" "admin_policy" {
  role       = aws_iam_role.lambda_exec_role1.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

## Lambda 4
resource "aws_lambda_function" "http_requester" {
  function_name = "HttpRequesterFunction"
  handler = "lambda_function.lambda_handler"
  runtime = "python3.12"
  role = aws_iam_role.lambda_exec_role1.arn
  filename         = "${path.module}/../../sample-apps2/build4/function.zip"
  timeout = 70

  tracing_config {
    mode = "Active"
  }
  environment {
    variables = {
      API_URL_1       = ""
      API_URL_2       = "https://9i6obftaoa.execute-api.us-west-1.amazonaws.com/prod/list?owners=lw&petid=dog"
      API_URL_3       = "https://9i6obftaoa.execute-api.us-west-1.amazonaws.com/prod/get?owners=lw&petid=dog&recordId=1"
      AWS_LAMBDA_EXEC_WRAPPER1 = "/opt/otel-instrument"
      OTEL_TRACES_EXPORTER = "console,otlp"
      OTEL_PYTHON_DISABLED_INSTRUMENTATIONS = "none"
    }
  }
}

### EventBridge
resource "aws_cloudwatch_event_rule" "every_minute" {
  name                = "TriggerHttpRequesterEveryMinute"
  schedule_expression = "rate(1 minute)"
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.http_requester.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.every_minute.arn
}


resource "aws_cloudwatch_event_target" "trigger_lambda" {
  rule      = aws_cloudwatch_event_rule.every_minute.name
  target_id = "HttpRequesterLambda"
  arn       = aws_lambda_function.http_requester.arn
}