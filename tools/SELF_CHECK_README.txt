Run your server first, then:
App1 (Sales CRM):
  python tools/selfcheck.py http://127.0.0.1:8000 /login admin admin /admin?tab=leads /admin?tab=calendar
  python tools/selfcheck.py http://127.0.0.1:8000 /login employee employee /employee?tab=leads

App2 (SayF Agency):
  python tools/selfcheck.py http://127.0.0.1:8000 /admin/login admin admin /admin?tab=leads /admin?tab=clients
  python tools/selfcheck.py http://127.0.0.1:8000 /client/login DemenageursPlus admin /client?tab=leads
