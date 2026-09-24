# Flow: `GET /{id}/with-departments-and-employees`

**Service (entry point):** `organization-service`
**Baseline commit:** `3337ad7` of `piomin/sample-spring-microservices-new` (api-service HEAD at doc generation time)
**Tier:** Middleware / orchestrating API service
**Classification:** API (synchronous, REST, service-to-service via OpenFeign)

---

## 1. Overview

This endpoint returns a single `Organization`, fully hydrated with its `Department` list, and — for every department — the `Employee` list working in that department. It looks like a single-hop call from the outside, but it is actually a **3-tier fan-out**: `organization-service` calls `department-service`, which in turn calls `employee-service` per department. A caller reading only `organization-service`'s code would incorrectly assume this is a 2-service interaction.

---

## 2. Request entry point

| | |
|---|---|
| Method | `GET` |
| Path | `/{id}/with-departments-and-employees` |
| Handler | [`OrganizationController.findByIdWithDepartmentsAndEmployees`](../api-service/organization-service/src/main/java/pl/piomin/services/organization/controller/OrganizationController.java:60) |
| Path param | `id` — organization ID (`Long`) |
| Request body | none |
| Auth | none present in code (no security filter/annotation observed — flag this, see §6) |

---

## 3. Call flow (hop by hop)

```
Client
  │
  ▼
[1] organization-service : GET /{id}/with-departments-and-employees
      OrganizationController.findByIdWithDepartmentsAndEmployees(id)
      │
      ├─ organization = repository.findById(id)          (in-memory List, NOT a real DB)
      │
      ▼
[2] department-service : GET /organization/{organizationId}/with-employees   (via Feign: DepartmentClient)
      DepartmentController.findByOrganizationWithEmployees(organizationId)
      │
      ├─ departments = repository.findByOrganization(organizationId)   (in-memory List)
      │
      └─ for EACH department:
           ▼
[3]      employee-service : GET /department/{departmentId}   (via Feign: EmployeeClient)
           returns List<Employee> for that department
      │
      └─ department.setEmployees(employees)      ← mutation happens INSIDE department-service
      │
      ▼
   returns List<Department> (each with employees populated) back to organization-service
      │
      └─ organization.setDepartments(departments)
      │
      ▼
   organization-service returns fully hydrated Organization to client
```

**Fan-out characteristic:** step [3] executes once **per department** returned in step [2] — sequentially, inside a `forEach` (`DepartmentController.java:53`). This is an **N+2 call pattern**: 1 call to department-service, then N calls to employee-service (one per department), all synchronous and blocking. There is no batching endpoint on employee-service (e.g. "give me employees for these department IDs") — each department triggers its own round trip.

---

## 4. Payload contracts

**Response shape returned to the client** (`Organization`, [model](../api-service/organization-service/src/main/java/pl/piomin/services/organization/model/Organization.java)):
```json
{
  "id": 1,
  "name": "string",
  "address": "string",
  "departments": [
    {
      "...department fields...": "...",
      "employees": [ { "...employee fields...": "..." } ]
    }
  ],
  "employees": []
}
```
Note: the top-level `employees` field on `Organization` is **always empty** for this specific endpoint — it's only populated by the sibling endpoint `/with-employees` (a different handler, §6). A consumer of this API needs to know to look inside `departments[].employees`, not the top-level field, or they will silently get an empty array.

**Inter-service contracts** are defined as Feign client interfaces, not a shared schema/OpenAPI contract:
- `organization-service` → `department-service`: [`DepartmentClient`](../api-service/organization-service/src/main/java/pl/piomin/services/organization/client/DepartmentClient.java)
- `department-service` → `employee-service`: [`EmployeeClient`](../api-service/department-service/src/main/java/pl/piomin/services/department/client/EmployeeClient.java)

Each service defines its **own copy** of the DTO shape it expects back (`Department`, `Employee` model classes exist independently in both `organization-service` and `department-service` packages). This is a duplication risk: if `employee-service` changes its response shape, both `department-service`'s and `organization-service`'s local copies of the model need manual updates — there's no shared contract enforcing consistency.

---

## 5. Error handling & resilience

- **No explicit error handling** in `OrganizationController` or `DepartmentController` for the downstream Feign calls — no `try/catch`, no fallback, no circuit breaker (no Resilience4j/Hystrix annotations observed on these client interfaces).
- `OrganizationRepository.findById` calls `.orElseThrow()` with **no argument** ([`OrganizationRepository.java:22`](../api-service/organization-service/src/main/java/pl/piomin/services/organization/repository/OrganizationRepository.java:22)) — this throws a bare `NoSuchElementException` with no message, which will surface as a generic 500 to the caller, not a 404. There's no `@ExceptionHandler` in this controller to translate it.
- If `department-service` is down or slow, the entire call chain blocks — no timeout override visible on the Feign client, so behavior depends on default Feign/OpenFeign timeout config (likely defined at the application/gateway level, not shown in this file — would need to check `application.yml`/gateway config to confirm actual timeout values).
- If employee-service fails for **one** department mid-`forEach`, the exception propagates and the entire response fails — a caller gets nothing back, not a partial result with the departments that did succeed.

---

## 6. Notable risk points / things a first-time reader would miss

1. **This is not a 2-service call, it's 3.** Reading `organization-service` in isolation, you'd think the department/employee data comes from one downstream call. It doesn't — `department-service` silently makes its own N downstream calls to `employee-service` as part of serving `organization-service`'s request.
2. **In-memory storage, not a real datastore.** Both `OrganizationRepository` and (presumably) `DepartmentRepository`/`EmployeeRepository` use `List<T>` in memory, not a database. This is a demo-repo artifact — in a real production service this would be a DB call, changing the failure modes (DB timeout, connection pool exhaustion) significantly. **Flagging this explicitly because it's the single biggest way this POC repo differs from a real prod service** — worth keeping in mind when judging how well this doc generalizes.
3. **No auth/authz visible** on any of the three endpoints in this chain.
4. **No batching** — the N+2 pattern means this endpoint's latency scales linearly with department count, and a single slow/failed employee-service call fails the whole request.
5. **Duplicated DTOs** across service boundaries with no shared contract — a classic "hidden coupling" that's easy to miss without tracing the actual field usage across repos.
6. **Service discovery is NOT resolvable from this repo alone.** Both `organization-service` and `department-service` import shared config from an external `config-service` at startup (`application.yml`: `spring.config.import: optional:configserver:http://config-service:8088`). This is almost certainly where Eureka/discovery registration and routing details actually live. Any doc for this flow is incomplete without also pulling `config-service`'s repo — flagging this as a structural gap in the trace, not just a detail we chose to omit.
7. **Not thread-safe.** `OrganizationRepository` and `DepartmentRepository` back their storage with a plain `ArrayList` (not `Collections.synchronizedList` or a concurrent collection). Concurrent requests hitting `add()` alongside `findAll()`/`findById()` risk lost writes or a `ConcurrentModificationException`. This would not surface in a real prod deployment (which would use a real DB), but is a correctness gap in this demo code worth noting since it affects how literally the doc should be trusted for "production behavior."
8. **Zero test coverage for this exact endpoint.** [`OrganizationControllerTests.java`](../api-service/organization-service/src/test/java/pl/piomin/services/organization/OrganizationControllerTests.java) tests `findAll`, `findById`, `findByIdWithDepartments`, and `add` — but has **no test** for `findByIdWithDepartmentsAndEmployees`, the flow this doc covers. If this endpoint breaks, nothing in CI would catch it.

---

## 7. Surface manifest (for future Diff Analyzer scoping)

Files that define this flow's boundary — a change to any of these should trigger doc re-validation:
- `organization-service/.../controller/OrganizationController.java`
- `organization-service/.../client/DepartmentClient.java`
- `organization-service/.../model/Organization.java`
- `department-service/.../controller/DepartmentController.java`
- `department-service/.../client/EmployeeClient.java`
- `department-service/.../model/Department.java`
- `organization-service/src/main/resources/application.yml`, `department-service/src/main/resources/application.yml` (external config-service import — routing/discovery config lives outside this repo, see §6 item 6)
