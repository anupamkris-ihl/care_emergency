# care_emergency documents

## International registration

- [International_Registration_Report.html](international/International_Registration_Report.html): the full report in one page (decisions, fields and rules, error messages, database, APIs, test cases, files changed, how to run).
- [International_Registration_Report.xlsx](international/International_Registration_Report.xlsx): the same content as an Excel workbook, one sheet per section. The Test Cases sheet has Pass/Fail columns for testers.

## Other reports

- [Emergency_New_Fields_Report.html](reports/Emergency_New_Fields_Report.html): emergency chief complaint, mode of arrival and MLC changes.
- [Care_Emergency_API_Reference.html](reports/Care_Emergency_API_Reference.html): every plug and Care core API used by registration, with error messages.
- [Care_Emergency_Swagger.html](reports/Care_Emergency_Swagger.html): Swagger UI for the same APIs (needs internet for the Swagger UI script).
- [care_emergency_openapi.json](reports/care_emergency_openapi.json): OpenAPI 3 spec; import into Postman and set `baseUrl` to the EC2 (`http://3.110.222.22:9000`) or local (`http://localhost:9000`) backend.
- [HMIS_October_Delivery_Plan.html](reports/HMIS_October_Delivery_Plan.html): October delivery plan, timeline and hours.

The HTML files open directly in a browser after downloading.
