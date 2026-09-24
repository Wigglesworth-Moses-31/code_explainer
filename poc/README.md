# POC: Code Flow Documentation

Input repos for the POC are third-party open-source projects. They are **not committed here and nothing clones them at runtime**. Place a copy in a local system directory yourself and reference it by path; the analysis only reads from that directory.

| Tier | Local folder (expected) | Upstream | Baseline commit |
|---|---|---|---|
| Middleware API service (Java / Spring Boot) | `poc/api-service` | https://github.com/piomin/sample-spring-microservices-new | `3337ad7` |
| Event dispatcher + Lambda consumer (Node.js / AWS SAM) | `poc/event-lambda-service` | https://github.com/aws-samples/iot-lambda-sns-sqs-lambda-dynamodb (MIT-0) | not recorded |

Relative links inside `poc/docs/` (e.g. `../api-service/...`) resolve only when the copies sit in the folders above.

## Docs
- `docs/organization-service_with-departments-and-employees_flow.md`: deep-dive of one API flow, including its surface manifest.
