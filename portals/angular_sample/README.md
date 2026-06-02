# Angular Material sample portal

Static-HTML replica of the Frame TV / Frame 2.0 admin portal's
"Country Content Mapping" page. Produces a DOM indistinguishable
from the real Angular Material build (same tag names, class names,
nesting from `tmp/portaldata/page-filled.html`), but with no
Angular runtime — vanilla JS, served by a tiny Node HTTP server.

The grabber only sees the DOM, so for label-capture / fingerprinting
purposes this is the same as the real portal.

## Run

```
node portals/angular_sample/mock_backend.js
```

Then open `http://localhost:5189/transfer-artwork` in a browser.
Default credentials are pre-filled (`tech@parallelloop.ai` / `demo`);
any non-empty username works.

Set `PORT` to override the default 5189 (the React sample portal
uses 5188).

## Page anatomy

| Widget                | DOM root                                 | Mirrors real portal lines |
|-----------------------|------------------------------------------|---------------------------|
| Year picker           | `mat-select#mat-select-year`             | 263-359                   |
| Target Make picker    | `mat-select#mat-select-make`             | (Year structure repeated) |
| Target Model picker   | `mat-select#mat-select-model`            | 360-455                   |
| Country/Region picker | `ng-multiselect-dropdown`                | 626-810                   |
| Show Data button      | `button.copyrightbtn[data-action=show-data]` | 811-824               |
| Source list           | `cdk-virtual-scroll-viewport[data-role=left-viewport]` | 984-2333 |
| Transfer Right button | `button[data-action=transfer-right]`     | 2339-2353                 |
| Target list           | `cdk-virtual-scroll-viewport[data-role=right-viewport]` | 2354+    |

## Endpoints

| Method | Path                                            | Purpose                                     |
|--------|-------------------------------------------------|---------------------------------------------|
| POST   | `/api/auth/login`                               | Bearer token issuance                       |
| GET    | `/api/makes`                                    | List of TV brands                           |
| GET    | `/api/models?make=&year=`                       | Cascading model list                        |
| GET    | `/api/countries?q=&make=&model=`                | Server-side country search (~300 ms latency) |
| GET    | `/api/show-data?country=&year=&make=&model=`    | Returns ~200 artwork rows                   |
| POST   | `/api/transfer`                                 | Moves checked rows to Target list           |
