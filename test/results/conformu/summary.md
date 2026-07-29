# ConformU summary

- ConformU: 2.0.0.16486 (locally installed 64-bit executable)
- Protocol URI: `http://127.0.0.1:11113/api/v1/camera/0`
- Camera V4 URI: `http://127.0.0.1:11114/api/v1/camera/0`
- Profiles: Alpaca Protocol and Camera interface conformance
- Protocol authoritative final log counts: `{'errors': 0, 'issues': 34, 'information': 4}`
- Camera V4 authoritative machine-result counts: `{'errors': 0, 'issues': 17, 'configuration_alerts': 0}`
- Camera V4 issue groups: `{'ImageArrayVariant': 16, 'PercentCompleted Read': 1}`
- Protocol 34 issues are one old-tool conflict repeated across requests: 17 capitalised PUT parameter names were accepted, followed by 17 paired ClientTransactionID complaints. Alpaca API V12 (23 February 2026) says parameter names are not case sensitive, so accepting these requests and echoing ClientTransactionID 67890 is required.
- Camera V4 ImageArrayVariant 16 issues are two old-tool messages for each of 8 bin modes. The current Master ICameraV4 definition says Alpaca cameras must raise PropertyNotImplementedException for this COM-only property.
- Camera V4 PercentCompleted 1 issue is an old-tool conflict. The current Master ICameraV4 definition requires InvalidOperationException whenever CameraState is inappropriate; the camera was idle.
- Current references: https://ascom-standards.org/api/ and https://ascom-standards.org/newdocs/camera.html
- Unexplained conformance findings: `0 errors, 0 issues`.
- protocol restoration: `verified: final snapshot exactly equals initial snapshot; errors=[]`
- camera-v4 restoration: `verified: final snapshot exactly equals initial snapshot; errors=[]`

Machine-readable result files and complete logs are retained beside this summary.
