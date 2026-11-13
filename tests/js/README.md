# Riders table interaction tests

Run the DOM tests in a disposable Node container. jsdom is installed only inside
that container; application dependencies remain unchanged.

```sh
docker run --rm --mount "type=bind,source=.,target=/workspace" --workdir /workspace node:22-slim sh -c 'npm install --prefix /tmp/rider-js-tests jsdom@26 && NODE_PATH=/tmp/rider-js-tests/node_modules node --test tests/js/rider_table.test.cjs'
```

These tests cover local filtering, column views, paging and sorting over 8,000
riders, selection limits and dragging after rendering, refresh failures and
namespace mismatches, safe HTML rendering, and saved controls. They do not make
provider requests or connect to a database. PostgreSQL endpoint and permission
coverage lives in `tests/test_rider_views.py`.
