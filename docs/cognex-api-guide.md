# Cognex API guide

Cognex gives you ten algorithms behind one HTTP endpoint: text similarity, vector search, machine learning, optimization, causal inference, computer vision, OCR, equation discovery, scheduling and physics simulation. Send one task or many in a single request; each task costs 1 credit.

## Quickstart

**1. Create an API key.** Sign in, open **API keys** in your dashboard and click **Create key**. The full key (it starts with `uak_`) is shown only once, so copy it somewhere safe.

**2. Store the key in an environment variable.** Every example in this guide reads it from `COGNEX_API_KEY`.

macOS / Linux / WSL / Git Bash:

```bash
export COGNEX_API_KEY="uak_your_key_here"
```

Windows PowerShell:

```powershell
$env:COGNEX_API_KEY = "uak_your_key_here"
```

**3. Send your first request.**

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "text.similarity",
      "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}
    }
  ]
}'
```

Response:

```json
{
  "results": [
    {
      "id": "1",
      "operation": "text.similarity",
      "status": "ok",
      "output": {"score": 96.2963, "scorer": "ratio"},
      "duration_ms": 0.4
    }
  ]
}
```

The curl examples run in macOS and Linux terminals, WSL and Git Bash. On Windows PowerShell, use the [PowerShell example](#powershell) instead.

## Basics

| | |
|---|---|
| **Base URL** | `https://vendor-production-41cd558f.up.railway.app` |
| **Run tasks** | `POST /v1/execute` |
| **List operations** | `GET /v1/operations` (public, no key needed) |
| **Authentication** | Header `X-API-Key: <your key>` on every `/v1/execute` call |
| **Format** | JSON in, JSON out (`Content-Type: application/json`) |
| **Cost** | 1 credit per task |
| **Tasks per request** | 1 to 32 |

### Keep your key secret

- Call the API from your server or from scripts, never from code that runs in a browser or a mobile app: anyone who has your key can spend your credits.
- Don't commit keys to Git. Read them from an environment variable or a secrets manager.
- If a key leaks, revoke it under **API keys** in your dashboard. It stops working on the next request; your usage history stays.
- Create one key per app or environment, so you can revoke one without affecting the others.

### Request format

```json
{
  "tasks": [
    {"id": "your-id", "operation": "text.similarity", "inputs": {"a": "...", "b": "..."}, "timeout_s": 10}
  ]
}
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Your label for the task, unique within the request. It is returned with the result. |
| `operation` | yes | One of the operation names below. |
| `inputs` | yes | The operation's inputs (see each operation). |
| `timeout_s` | no | Seconds to allow; defaults to the operation's default and is capped at its maximum. |

All tasks in a request run in parallel. Results are returned in the same order as the tasks, and one failed task never fails the others.

### Response format

```json
{
  "results": [
    {"id": "your-id", "operation": "text.similarity", "status": "ok", "output": {"score": 96.2963, "scorer": "ratio"}, "duration_ms": 0.4}
  ]
}
```

A failed task has `"status": "error"` with `error_type` and `error` in place of `output`:

```json
{"id": "your-id", "operation": "text.similarity", "status": "error", "error_type": "invalid_input", "error": "missing required input 'b'", "duration_ms": 0.1}
```

| `error_type` | Meaning | What to do |
|---|---|---|
| `invalid_input` | The inputs were missing or not valid for the operation. | Fix the inputs; the message says which one. |
| `unknown_operation` | The operation name doesn't exist. | Check the name against the list below. Not charged. |
| `timeout` | The task ran longer than its timeout. | Send a smaller problem or raise `timeout_s` (up to the maximum). |
| `operation_failed` | The algorithm raised an error on these inputs. | Check that the inputs make sense for the problem. |
| `worker_crashed` | The engine stopped unexpectedly. It restarts automatically. | Retry the task. |

### HTTP status codes

| Status | Meaning |
|---|---|
| `200` | The request ran. Check each task's `status`. |
| `401` | Missing, invalid or revoked API key. |
| `402` | Not enough credits. Nothing ran and nothing was charged. |
| `422` | The request body isn't valid (for example, no tasks, more than 32 tasks, or duplicate task ids). |
| `503` | Temporarily unavailable. Retry after a short wait. |

## Credits

- **Each task costs 1 credit.** A request with 10 tasks costs 10 credits. A task with an unknown operation name costs nothing.
- **The whole request is reserved before anything runs.** If your balance is lower than the request's cost, you get HTTP `402` and nothing runs:

  ```json
  {"error": "insufficient_credits", "message": "this request needs 10 credit(s) but the available balance is 4", "required": 10, "balance": 4}
  ```

- **A task that starts is charged, even if it ends with an error** (for example `invalid_input` or `timeout`), because it used compute. A task that never got to start is refunded automatically.
- **Welcome credits are used first** and don't expire. **Purchased credits expire 12 months after purchase**; the ones expiring soonest are used first. Expired credits can't be used.
- Your balance, next expiration date and usage per key are in your dashboard.

## Operations

| Operation | What it does | Credits | Default / max timeout |
|---|---|---|---|
| [`text.similarity`](#textsimilarity) | Scores how similar two strings are (0 to 100) | 1 | 5 s / 10 s |
| [`vectors.nearest_neighbors`](#vectorsnearest_neighbors) | Finds the closest vectors to each query vector | 1 | 30 s / 120 s |
| [`tabular.train_and_predict`](#tabulartrain_and_predict) | Trains a model on your rows and predicts new rows | 1 | 60 s / 300 s |
| [`optimization.solve_convex`](#optimizationsolve_convex) | Solves linear, quadratic and conic optimization problems | 1 | 30 s / 300 s |
| [`causal.estimate_effect`](#causalestimate_effect) | Estimates a treatment's causal effect on an outcome | 1 | 60 s / 300 s |
| [`image.detect_edges`](#imagedetect_edges) | Finds the edges in an image | 1 | 60 s / 120 s |
| [`image.extract_text`](#imageextract_text) | Reads the text in an image (OCR) | 1 | 120 s / 300 s |
| [`math.discover_equation`](#mathdiscover_equation) | Finds a readable formula that fits your data | 1 | 300 s / 900 s |
| [`scheduling.school_timetable`](#schedulingschool_timetable) | Assigns lessons to timeslots and rooms without conflicts | 1 | 60 s / 180 s |
| [`physics.simulate_rigid_bodies`](#physicssimulate_rigid_bodies) | Simulates 3-D objects falling and colliding | 1 | 30 s / 120 s |

The same list is available as JSON from `GET /v1/operations`. If an operation hasn't been used for a while, its first request can take longer while its engine starts.

---

### `text.similarity`

Scores how similar two strings are, from 0 (nothing in common) to 100 (identical), using normalized Indel similarity. Useful for matching product names, deduplicating records and fuzzy search.

| Input | Type | Description |
|---|---|---|
| `a` | string, required | First string |
| `b` | string, required | Second string |

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "text.similarity",
      "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "text.similarity",
      "status": "ok",
      "output": {"score": 96.2963, "scorer": "ratio"},
      "duration_ms": 0.4
    }
  ]
}
```

---

### `vectors.nearest_neighbors`

Builds a search index over your vectors and returns the `k` nearest ones to each query, using approximate (HNSW) search. Useful for semantic search over embeddings, recommendations and similarity lookups.

| Input | Type | Description |
|---|---|---|
| `vectors` | list of number lists, required | The vectors to search, all the same length (up to 200,000 vectors of up to 4,096 dimensions) |
| `queries` | list of number lists, required | Vectors to find neighbours for (up to 10,000) |
| `k` | integer, default 1 | How many neighbours to return per query |
| `space` | `"l2"` (default), `"ip"` or `"cosine"` | Distance: Euclidean (squared), inner product or cosine |
| `ids` | list of integers, optional | Your own non-negative ids, one per vector (defaults to 0, 1, 2, …) |

Output: `labels[i]` lists the ids of the neighbours of query `i`, nearest first, and `distances[i]` their distances.

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "vectors.nearest_neighbors",
      "inputs": {"vectors": [[0, 0], [1, 0], [0, 1], [5, 5]], "queries": [[0.9, 0.1]], "k": 2}
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "vectors.nearest_neighbors",
      "status": "ok",
      "output": {"labels": [[1, 0]], "distances": [[0.02, 0.82]], "space": "l2", "k": 2},
      "duration_ms": 3.4
    }
  ]
}
```

---

### `tabular.train_and_predict`

Trains a gradient-boosted tree model on your rows (`X`, `y`) and predicts the rows in `X_predict` in the same call. Use `"task": "regression"` to predict numbers, or `"classification"` to predict classes. Nothing is stored: the model exists only for the duration of the call.

| Input | Type | Description |
|---|---|---|
| `X` | list of number lists, required | Training rows (features) |
| `y` | list of numbers, required | Target for each training row. For classification: class labels 0, 1, …, k-1 |
| `X_predict` | list of number lists, required | Rows to predict, with the same number of features as `X` |
| `task` | `"regression"` (default) or `"classification"` | |
| `n_estimators` | integer, default 100 | Number of trees (1 to 2,000) |
| `max_depth` | integer, default 3 | Maximum tree depth (1 to 16) |
| `learning_rate` | number, default 0.1 | |
| `random_state` | integer, default 0 | Seed, for repeatable results |

Output: `predictions` (one per row of `X_predict`), `probabilities` for classification, and `feature_importances`.

This example predicts whether a customer will upgrade (1) or not (0) from two features:

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "tabular.train_and_predict",
      "inputs": {
        "task": "classification",
        "X": [
          [1, 4],
          [2, 5],
          [2, 8],
          [3, 6],
          [4, 7],
          [5, 5],
          [6, 8],
          [7, 6],
          [8, 7],
          [9, 8],
          [1, 9],
          [8, 3]
        ],
        "y": [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 1],
        "X_predict": [[1.5, 6], [8.5, 6]],
        "n_estimators": 50
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "tabular.train_and_predict",
      "status": "ok",
      "output": {
        "task": "classification",
        "n_train": 12,
        "n_features": 2,
        "predictions": [0, 1],
        "probabilities": [[0.7987, 0.2013], [0.2013, 0.7987]],
        "feature_importances": [1.0, 0.0]
      },
      "duration_ms": 199.0
    }
  ]
}
```

---

### `optimization.solve_convex`

Solves convex optimization problems of the form

> minimize ½·xᵀPx + cᵀx  subject to  Ax + s = b, s ∈ K

This covers linear programs, quadratic programs, second-order cone programs and semidefinite programs. `K` is described by `cone`, and the rows of `A` and `b` must be ordered by cone: `z` (equalities), `l` (inequalities, `Ax ≤ b`), `q` (second-order cones), `s` (semidefinite), `ep`, `ed` (exponential cones).

| Input | Type | Description |
|---|---|---|
| `c` | list of numbers, required | Linear objective |
| `A` | list of number lists, required | Constraint matrix, one row per constraint |
| `b` | list of numbers, required | Constraint right-hand side |
| `cone` | object, required | Number of rows of each cone type, e.g. `{"z": 1, "l": 4}` or `{"l": 2, "q": [3]}` |
| `P` | symmetric matrix, optional | Quadratic objective (for quadratic programs) |
| `eps_abs`, `eps_rel` | numbers, default 1e-6 | Accuracy |
| `max_iters` | integer, default 100,000 | |

Output: `status` (`"solved"` when an optimum was found), the solution `x`, `objective`, and the dual values `y` and slacks `s`.

This example maximizes x + y (minimizes −x − y) subject to x + 2y ≤ 4, 3x + y ≤ 6, x ≥ 0, y ≥ 0. The optimum is x = 1.6, y = 1.2:

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "optimization.solve_convex",
      "inputs": {
        "c": [-1, -1],
        "A": [[1, 2], [3, 1], [-1, 0], [0, -1]],
        "b": [4, 6, 0, 0],
        "cone": {"l": 4}
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "optimization.solve_convex",
      "status": "ok",
      "output": {
        "status": "solved",
        "status_val": 1,
        "x": [1.6, 1.2],
        "y": [0.4, 0.2, 0.0, 0.0],
        "s": [0.0, 0.0, 1.6, 1.2],
        "objective": -2.8,
        "dual_objective": -2.8,
        "iterations": 50,
        "solve_time_ms": 0.0985
      },
      "duration_ms": 1.6
    }
  ]
}
```

---

### `causal.estimate_effect`

Estimates the average causal effect of a treatment on an outcome from observational data, adjusting for the confounders you name, so the answer is the effect of the treatment rather than a plain correlation.

| Input | Type | Description |
|---|---|---|
| `data` | object, required | Columns: `{"column name": [numbers…]}`, all the same length (10 to 200,000 rows) |
| `treatment` | string, required | Treatment column |
| `outcome` | string, required | Outcome column |
| `common_causes` | list of strings | Confounders: columns that affect both treatment and outcome |
| `instruments` | list of strings | Instrumental variables (for `iv.instrumental_variable`) |
| `method` | string, default `"backdoor.linear_regression"` | Also: `backdoor.propensity_score_matching`, `backdoor.propensity_score_stratification`, `backdoor.propensity_score_weighting` (these need a 0/1 treatment), `iv.instrumental_variable` |

Output: `effect`, the estimated change in the outcome per unit of treatment.

In this example, loyal customers get bigger discounts *and* spend more anyway. A plain correlation would overstate the discount's effect; adjusting for `loyalty` recovers the true effect (3 per unit of discount):

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "causal.estimate_effect",
      "inputs": {
        "data": {
          "discount": [2.7, 5.7, 5.0, 4.6, 3.7, 5.5, 2.0, 4.3, 3.2, 2.6, 4.0, 1.9, 2.5, 1.8, 5.3, 2.8, 5.1, 6.7, 4.5, 7.3, 3.4, 3.6, 2.8, 2.3],
          "spend": [20.7, 34.9, 31.2, 18.6, 16.7, 33.9, 6.4, 29.2, 25.8, 17.2, 18.2, 11.7, 12.3, 14.5, 25.8, 19.4, 34.9, 35.7, 25.8, 42.0, 14.9, 13.6, 20.4, 7.9],
          "loyalty": [6.3, 9.0, 7.8, 2.3, 3.0, 8.7, 0.1, 8.2, 8.0, 4.7, 3.0, 2.8, 2.5, 4.5, 5.0, 5.5, 10.0, 7.9, 6.2, 9.9, 2.2, 1.6, 6.1, 0.4]
        },
        "treatment": "discount",
        "outcome": "spend",
        "common_causes": ["loyalty"]
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "causal.estimate_effect",
      "status": "ok",
      "output": {
        "effect": 2.9898,
        "method": "backdoor.linear_regression",
        "treatment": "discount",
        "outcome": "spend",
        "estimand_type": "EstimandType.NONPARAMETRIC_ATE",
        "n_rows": 24
      },
      "duration_ms": 18.6
    }
  ]
}
```

---

### `image.detect_edges`

Finds the edges in an image using the Canny algorithm. Returns the number of edge pixels and, by default, a black-and-white PNG of the edges.

| Input | Type | Description |
|---|---|---|
| `image_base64` | string, required | The image (PNG, JPEG, …) as base64, optionally as a `data:image/...;base64,` URL. Up to 16 megapixels |
| `low_threshold`, `high_threshold` | numbers, default 0.1 and 0.2 | Edge strength thresholds (0 to 1) |
| `kernel_size` | odd integer, default 5 | Blur kernel size |
| `sigma` | number, default 1.0 | Blur strength |
| `return_image` | boolean, default true | Set `false` to get only the statistics |

Output: `edge_pixels`, `edge_fraction`, `width`, `height` and `edges_png_base64` (edges in white on black).

This example sends a 48×48 PNG of a white square on black:

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "image.detect_edges",
      "inputs": {
        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAAAAAByaaZbAAAAJ0lEQVR42mNgGAWjgDqAEZnznwhFTKTaMKphVMOohlENo2AUDAwAALq1ATA8CXn2AAAAAElFTkSuQmCC"
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "image.detect_edges",
      "status": "ok",
      "output": {
        "width": 48,
        "height": 48,
        "edge_pixels": 92,
        "edge_fraction": 0.0399,
        "edges_png_base64": "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAAAAABy…"
      },
      "duration_ms": 20.5
    }
  ]
}
```

To send your own image file: `base64 -w0 photo.png` (Linux), `base64 -i photo.png` (macOS) or `[Convert]::ToBase64String([IO.File]::ReadAllBytes("photo.png"))` (PowerShell).

---

### `image.extract_text`

Reads printed text in an image (OCR) and returns each line with a confidence score and bounding box.

| Input | Type | Description |
|---|---|---|
| `image_base64` | string, required | The image as base64 (see `image.detect_edges`). Up to 16 megapixels |
| `lang` | string, default `"en"` | `en`, `ch`, `fr`, `german`, `es`, `it`, `pt`, `ru`, `japan` or `korean` |

Output: `text` (all lines joined with newlines) and `lines`, each with `text`, `confidence` (0 to 1) and `box` (`[x_min, y_min, x_max, y_max]` in pixels).

This example sends a 200×44 PNG that says "Hello Cognex":

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "image.extract_text",
      "inputs": {
        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAMgAAAAsAQAAAAAI9sqnAAABZklEQVR42u2TwUrDQBCG/00LBkFa78KuvoEXaaCYUDz4GH0C0SdIoKDX+gTtYwRBSEWoB6F9g65QaG5urIeNbDMe2sS0kIs3wf80MHzzz87sMEKFLPxnfpf5gMBXDOAt2MlYEOtwONyGKCFOyZiIeIPKsmABsAVAItyplm2qZd5OC7QkPkrGyjf0rvyec2c450REoIRcpIvINUSRixZTjYGb+8zaASbSAJNZ21PL8H3js2LohLJ5DaB/1Qn36kdna0dSaPvOgk80EX/NnHS8nG+6ZiQDQKAOCAA1G6aYAZiW0AaAZjBJ87D0nljAriOGTR5DpgpGkoTUCIzULKw1MioYBFoKG5gL27eNxjzImRNcim5kRXb3CWDic79g5CMQ99nUi/sPqJlpFOUMe25JR+Cm6Ry/eObg4l7m+3FH6UIRSV/R7WlquDtYT3RLmV/aXFmryr9jnf/EbOsayaIKhvWqmL9yJd+ZxNGFwTTURwAAAABJRU5ErkJggg=="
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "image.extract_text",
      "status": "ok",
      "output": {
        "lines": [{"text": "Hello Cognex", "confidence": 0.9999, "box": [5, 7, 167, 39]}],
        "text": "Hello Cognex",
        "lang": "en"
      },
      "duration_ms": 2704.0
    }
  ]
}
```

---

### `math.discover_equation`

Searches for a readable mathematical formula that fits your data (symbolic regression). Instead of a black-box model, you get an equation such as `x0*x0 + 2.0`, plus simpler and more complex alternatives.

| Input | Type | Description |
|---|---|---|
| `X` | list of number lists, required | Input rows; columns are named `x0`, `x1`, … (5 to 10,000 rows, up to 20 columns) |
| `y` | list of numbers, required | Value to explain, one per row |
| `binary_operators` | list, default `["+", "-", "*", "/"]` | Any of `+ - * / ^ max min` |
| `unary_operators` | list, default `[]` | Any of `sin cos exp log sqrt abs square cube neg inv tanh` |
| `niterations` | integer, default 20 | More iterations search longer (1 to 500) |
| `maxsize` | integer, default 20 | Maximum equation size |
| `random_state` | integer, default 0 | Seed, for repeatable results |

Output: `best` (the equation with the best fit for its complexity, with its `loss`) and `pareto_front` (the best equation at each complexity).

This search can take from several seconds to a few minutes. The example below rediscovers y = x² + 2:

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "math.discover_equation",
      "inputs": {
        "X": [[-2], [-1.5], [-1], [-0.5], [0], [0.5], [1], [1.5], [2], [2.5]],
        "y": [6, 4.25, 3, 2.25, 2, 2.25, 3, 4.25, 6, 8.25],
        "niterations": 15,
        "random_state": 0
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "math.discover_equation",
      "status": "ok",
      "output": {
        "best": {
          "equation": "x0*x0 + 2.0",
          "julia": "(x0 * x0) + 2.0",
          "loss": 0.0,
          "complexity": 5
        },
        "pareto_front": [
          {"equation": "4.12499950000000", "loss": 3.8156, "complexity": 1},
          {"equation": "x0 - 1*(-3.874997)", "loss": 3.8156, "complexity": 3},
          {"equation": "x0*x0 + 2.0", "loss": 0.0, "complexity": 5}
        ],
        "feature_names": ["x0"]
      },
      "duration_ms": 19147.2
    }
  ]
}
```

---

### `scheduling.school_timetable`

Assigns every lesson a timeslot and a room. Hard rules: no room, teacher or student group is booked twice at the same time. Soft rule: each teacher stays in one room where possible. The solver stops early when it finds a perfect timetable.

| Input | Type | Description |
|---|---|---|
| `timeslots` | list, required | `{"id", "day_of_week", "start_time", "end_time"}` (strings), up to 500 |
| `rooms` | list, required | `{"id", "name"}`, up to 200 |
| `lessons` | list, required | `{"id", "subject", "teacher", "student_group"}`, up to 2,000 |
| `time_limit_seconds` | number, default 5 | How long to search (1 to 120) |

Output: `assignments` (a timeslot and room for each lesson), `score`, and `feasible` (`true` when no hard rule is broken).

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "scheduling.school_timetable",
      "inputs": {
        "timeslots": [
          {
            "id": "mon-1",
            "day_of_week": "MONDAY",
            "start_time": "08:30",
            "end_time": "09:30"
          },
          {
            "id": "mon-2",
            "day_of_week": "MONDAY",
            "start_time": "09:30",
            "end_time": "10:30"
          }
        ],
        "rooms": [{"id": "a", "name": "Room A"}, {"id": "b", "name": "Room B"}],
        "lessons": [
          {
            "id": "math-9",
            "subject": "Math",
            "teacher": "A. Turing",
            "student_group": "9th"
          },
          {
            "id": "physics-9",
            "subject": "Physics",
            "teacher": "M. Curie",
            "student_group": "9th"
          },
          {
            "id": "chemistry-10",
            "subject": "Chemistry",
            "teacher": "M. Curie",
            "student_group": "10th"
          },
          {
            "id": "history-10",
            "subject": "History",
            "teacher": "I. Jones",
            "student_group": "10th"
          }
        ],
        "time_limit_seconds": 5
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "scheduling.school_timetable",
      "status": "ok",
      "output": {
        "score": "0hard/0soft",
        "hard_score": 0,
        "soft_score": 0,
        "feasible": true,
        "assignments": [
          {"lesson_id": "math-9", "timeslot_id": "mon-1", "room_id": "b"},
          {"lesson_id": "physics-9", "timeslot_id": "mon-2", "room_id": "a"},
          {"lesson_id": "chemistry-10", "timeslot_id": "mon-1", "room_id": "a"},
          {"lesson_id": "history-10", "timeslot_id": "mon-2", "room_id": "b"}
        ]
      },
      "duration_ms": 708.2
    }
  ]
}
```

---

### `physics.simulate_rigid_bodies`

Simulates 3-D rigid bodies (balls, boxes and capsules) under gravity, with collisions, friction and bounce, and returns where each body ends up.

| Input | Type | Description |
|---|---|---|
| `bodies` | list, required | Up to 2,000 bodies, see below |
| `steps` | integer, default 120 | Number of simulation steps |
| `timestep` | number, default 1/60 | Seconds per step |
| `gravity` | `[x, y, z]`, default `[0, -9.81, 0]` | |
| `record_every` | integer, default 0 | Also return every body's state every N steps (`trajectory`) |

Each body: `id`; `type` (`"dynamic"`, `"fixed"` or `"kinematic"`); `position` `[x, y, z]`; `shape`: `{"type": "ball", "radius"}`, `{"type": "cuboid", "half_extents": [x, y, z]}` or `{"type": "capsule", "half_height", "radius"}`; optional `linvel`, `angvel`, `restitution` (bounce, 0 to 1), `friction`, `density`.

Output: each body's final `position`, `rotation` (quaternion `[x, y, z, w]`), `linvel` and whether it is `sleeping` (at rest).

This example drops a ball of radius 0.5 from a height of 5 onto the ground; after 4 simulated seconds it rests at y ≈ 0.6:

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "1",
      "operation": "physics.simulate_rigid_bodies",
      "inputs": {
        "bodies": [
          {
            "id": "ground",
            "type": "fixed",
            "position": [0, 0, 0],
            "shape": {"type": "cuboid", "half_extents": [10, 0.1, 10]}
          },
          {
            "id": "ball",
            "type": "dynamic",
            "position": [0, 5, 0],
            "shape": {"type": "ball", "radius": 0.5}
          }
        ],
        "steps": 240
      }
    }
  ]
}'
```

```json
{
  "results": [
    {
      "id": "1",
      "operation": "physics.simulate_rigid_bodies",
      "status": "ok",
      "output": {
        "steps": 240,
        "timestep": 0.0167,
        "simulated_time": 4,
        "bodies": [
          {
            "id": "ground",
            "position": [0, 0, 0],
            "rotation": [0, 0, 0, 1],
            "linvel": [0, 0, 0],
            "sleeping": false
          },
          {
            "id": "ball",
            "position": [0, 0.5987, 0],
            "rotation": [0, 0, 0, 1],
            "linvel": [0, 0, 0],
            "sleeping": true
          }
        ]
      },
      "duration_ms": 25.3
    }
  ]
}
```

---

## All ten operations in one request

Tasks in one request run in parallel, so this returns about as fast as the slowest task (here `math.discover_equation`). It costs **10 credits**.

```bash
curl -sS https://vendor-production-41cd558f.up.railway.app/v1/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $COGNEX_API_KEY" \
  -d '{
  "tasks": [
    {
      "id": "similarity",
      "operation": "text.similarity",
      "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}
    },
    {
      "id": "neighbors",
      "operation": "vectors.nearest_neighbors",
      "inputs": {"vectors": [[0, 0], [1, 0], [0, 1], [5, 5]], "queries": [[0.9, 0.1]], "k": 2}
    },
    {
      "id": "prediction",
      "operation": "tabular.train_and_predict",
      "inputs": {
        "task": "classification",
        "X": [
          [1, 4],
          [2, 5],
          [2, 8],
          [3, 6],
          [4, 7],
          [5, 5],
          [6, 8],
          [7, 6],
          [8, 7],
          [9, 8],
          [1, 9],
          [8, 3]
        ],
        "y": [0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 0, 1],
        "X_predict": [[1.5, 6], [8.5, 6]],
        "n_estimators": 50
      }
    },
    {
      "id": "optimization",
      "operation": "optimization.solve_convex",
      "inputs": {
        "c": [-1, -1],
        "A": [[1, 2], [3, 1], [-1, 0], [0, -1]],
        "b": [4, 6, 0, 0],
        "cone": {"l": 4}
      }
    },
    {
      "id": "causal-effect",
      "operation": "causal.estimate_effect",
      "inputs": {
        "data": {
          "discount": [2.7, 5.7, 5.0, 4.6, 3.7, 5.5, 2.0, 4.3, 3.2, 2.6, 4.0, 1.9, 2.5, 1.8, 5.3, 2.8, 5.1, 6.7, 4.5, 7.3, 3.4, 3.6, 2.8, 2.3],
          "spend": [20.7, 34.9, 31.2, 18.6, 16.7, 33.9, 6.4, 29.2, 25.8, 17.2, 18.2, 11.7, 12.3, 14.5, 25.8, 19.4, 34.9, 35.7, 25.8, 42.0, 14.9, 13.6, 20.4, 7.9],
          "loyalty": [6.3, 9.0, 7.8, 2.3, 3.0, 8.7, 0.1, 8.2, 8.0, 4.7, 3.0, 2.8, 2.5, 4.5, 5.0, 5.5, 10.0, 7.9, 6.2, 9.9, 2.2, 1.6, 6.1, 0.4]
        },
        "treatment": "discount",
        "outcome": "spend",
        "common_causes": ["loyalty"]
      }
    },
    {
      "id": "edges",
      "operation": "image.detect_edges",
      "inputs": {
        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAAAAAByaaZbAAAAJ0lEQVR42mNgGAWjgDqAEZnznwhFTKTaMKphVMOohlENo2AUDAwAALq1ATA8CXn2AAAAAElFTkSuQmCC"
      }
    },
    {
      "id": "ocr",
      "operation": "image.extract_text",
      "inputs": {
        "image_base64": "iVBORw0KGgoAAAANSUhEUgAAAMgAAAAsAQAAAAAI9sqnAAABZklEQVR42u2TwUrDQBCG/00LBkFa78KuvoEXaaCYUDz4GH0C0SdIoKDX+gTtYwRBSEWoB6F9g65QaG5urIeNbDMe2sS0kIs3wf80MHzzz87sMEKFLPxnfpf5gMBXDOAt2MlYEOtwONyGKCFOyZiIeIPKsmABsAVAItyplm2qZd5OC7QkPkrGyjf0rvyec2c450REoIRcpIvINUSRixZTjYGb+8zaASbSAJNZ21PL8H3js2LohLJ5DaB/1Qn36kdna0dSaPvOgk80EX/NnHS8nG+6ZiQDQKAOCAA1G6aYAZiW0AaAZjBJ87D0nljAriOGTR5DpgpGkoTUCIzULKw1MioYBFoKG5gL27eNxjzImRNcim5kRXb3CWDic79g5CMQ99nUi/sPqJlpFOUMe25JR+Cm6Ry/eObg4l7m+3FH6UIRSV/R7WlquDtYT3RLmV/aXFmryr9jnf/EbOsayaIKhvWqmL9yJd+ZxNGFwTTURwAAAABJRU5ErkJggg=="
      }
    },
    {
      "id": "equation",
      "operation": "math.discover_equation",
      "inputs": {
        "X": [[-2], [-1.5], [-1], [-0.5], [0], [0.5], [1], [1.5], [2], [2.5]],
        "y": [6, 4.25, 3, 2.25, 2, 2.25, 3, 4.25, 6, 8.25],
        "niterations": 15,
        "random_state": 0
      }
    },
    {
      "id": "timetable",
      "operation": "scheduling.school_timetable",
      "inputs": {
        "timeslots": [
          {
            "id": "mon-1",
            "day_of_week": "MONDAY",
            "start_time": "08:30",
            "end_time": "09:30"
          },
          {
            "id": "mon-2",
            "day_of_week": "MONDAY",
            "start_time": "09:30",
            "end_time": "10:30"
          }
        ],
        "rooms": [{"id": "a", "name": "Room A"}, {"id": "b", "name": "Room B"}],
        "lessons": [
          {
            "id": "math-9",
            "subject": "Math",
            "teacher": "A. Turing",
            "student_group": "9th"
          },
          {
            "id": "physics-9",
            "subject": "Physics",
            "teacher": "M. Curie",
            "student_group": "9th"
          },
          {
            "id": "chemistry-10",
            "subject": "Chemistry",
            "teacher": "M. Curie",
            "student_group": "10th"
          },
          {
            "id": "history-10",
            "subject": "History",
            "teacher": "I. Jones",
            "student_group": "10th"
          }
        ],
        "time_limit_seconds": 5
      }
    },
    {
      "id": "physics",
      "operation": "physics.simulate_rigid_bodies",
      "inputs": {
        "bodies": [
          {
            "id": "ground",
            "type": "fixed",
            "position": [0, 0, 0],
            "shape": {"type": "cuboid", "half_extents": [10, 0.1, 10]}
          },
          {
            "id": "ball",
            "type": "dynamic",
            "position": [0, 5, 0],
            "shape": {"type": "ball", "radius": 0.5}
          }
        ],
        "steps": 240
      }
    }
  ]
}'
```

Response (outputs left out for brevity; each one matches the examples above):

```json
{
  "results": [
    {
      "id": "similarity",
      "operation": "text.similarity",
      "status": "ok",
      "duration_ms": 1.4
    },
    {
      "id": "neighbors",
      "operation": "vectors.nearest_neighbors",
      "status": "ok",
      "duration_ms": 3.6
    },
    {
      "id": "prediction",
      "operation": "tabular.train_and_predict",
      "status": "ok",
      "duration_ms": 30.8
    },
    {
      "id": "optimization",
      "operation": "optimization.solve_convex",
      "status": "ok",
      "duration_ms": 3.2
    },
    {
      "id": "causal-effect",
      "operation": "causal.estimate_effect",
      "status": "ok",
      "duration_ms": 17.4
    },
    {
      "id": "edges",
      "operation": "image.detect_edges",
      "status": "ok",
      "duration_ms": 19.9
    },
    {
      "id": "ocr",
      "operation": "image.extract_text",
      "status": "ok",
      "duration_ms": 640.2
    },
    {
      "id": "equation",
      "operation": "math.discover_equation",
      "status": "ok",
      "duration_ms": 552.5
    },
    {
      "id": "timetable",
      "operation": "scheduling.school_timetable",
      "status": "ok",
      "duration_ms": 41.0
    },
    {
      "id": "physics",
      "operation": "physics.simulate_rigid_bodies",
      "status": "ok",
      "duration_ms": 5.9
    }
  ]
}
```

## From code

### Python

Uses the [`requests`](https://pypi.org/project/requests/) package (`pip install requests`).

```python
import os
import requests

response = requests.post(
    "https://vendor-production-41cd558f.up.railway.app/v1/execute",
    headers={"X-API-Key": os.environ["COGNEX_API_KEY"]},
    json={"tasks": [
        {"id": "1", "operation": "text.similarity", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}},
    ]},
    timeout=300,
)
if response.status_code == 402:
    raise SystemExit("Not enough credits: " + response.json()["message"])
response.raise_for_status()
for result in response.json()["results"]:
    if result["status"] == "ok":
        print(result["id"], result["output"])
    else:
        print(result["id"], "failed:", result["error_type"], result["error"])
```

### JavaScript (Node.js 18 or later, server-side)

```javascript
const response = await fetch("https://vendor-production-41cd558f.up.railway.app/v1/execute", {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": process.env.COGNEX_API_KEY },
  body: JSON.stringify({
    tasks: [
      { id: "1", operation: "text.similarity", inputs: { a: "Drift Protein", b: "Drift Proteins" } },
    ],
  }),
});
if (response.status === 402) throw new Error("Not enough credits: " + (await response.json()).message);
if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
for (const result of (await response.json()).results) {
  if (result.status === "ok") console.log(result.id, result.output);
  else console.log(result.id, "failed:", result.error_type, result.error);
}
```

Save it as `cognex.mjs` and run `node cognex.mjs`.

### PowerShell

```powershell
$body = @{ tasks = @(
    @{ id = "1"; operation = "text.similarity"; inputs = @{ a = "Drift Protein"; b = "Drift Proteins" } }
) } | ConvertTo-Json -Depth 10
$response = Invoke-RestMethod -Method Post -Uri "https://vendor-production-41cd558f.up.railway.app/v1/execute" `
    -Headers @{ "X-API-Key" = $env:COGNEX_API_KEY } -ContentType "application/json" -Body $body
$response.results | ConvertTo-Json -Depth 10
```

## Open-source acknowledgements

Cognex is built with open-source software. See our [Open-source acknowledgements](open-source-acknowledgements) page for the projects we use and their licenses. The same list is available as JSON from `GET /v1/open-source`.
