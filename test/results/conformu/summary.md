# ConformU summary

- ConformU: 2.0.0.16486 (64-bit)
- Protocol URI: `http://127.0.0.1:11113/api/v1/camera/0`
- Camera V4 URI: `http://127.0.0.1:11114/api/v1/camera/0`
- Protocol final counts: `{'errors': 0, 'issues': 34, 'information': 4}`
- Allowed Protocol issue groups: `{'PUT form common-parameter casing': 34}`
- Camera V4 counts: `{'errors': 0, 'issues': 18, 'configuration_alerts': 0}`
- Allowed Camera V4 issue groups: `{'ImageArrayVariant': 16, 'PercentCompleted Read': 1, 'StopExposure': 1}`
- ImageArrayVariant is a COM-only property; an Alpaca camera must report PropertyNotImplemented.
- PercentCompleted is required to report InvalidOperation while the camera state is inappropriate (idle in this run).
- References: https://ascom-standards.org/api/ and https://ascom-standards.org/newdocs/camera.html
- Unexpected errors: `[]`
- Unexpected issues: `[]`
- Validation errors: `[]`
- protocol restoration: `{'present': True, 'restored': True, 'errors': [], 'snapshots_equal': True}`
- camera-v4 restoration: `{'present': True, 'restored': True, 'errors': [], 'snapshots_equal': True}`
- Overall result: `PASS`

Machine-readable result files and complete logs are retained beside this summary.
