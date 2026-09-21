from __future__ import annotations

SOURCES = [
    {"key": "wdfi", "name": "Wisconsin Department of Financial Institutions", "url": "https://apps.dfi.wi.gov/apps/corpsearch/search.aspx", "category": "Business status", "status": "ready"},
    {"key": "osha", "name": "OSHA Establishment Search", "url": "https://www.osha.gov/ords/imis/establishment.html", "category": "Safety / enforcement", "status": "ready"},
    {"key": "wcrb", "name": "Wisconsin Compensation Rating Bureau", "url": "https://www.wcrb.org/", "category": "Workers compensation", "status": "not_implemented"},
    {"key": "wcca", "name": "Wisconsin Circuit Court Access / CCAP", "url": "/wcca-workbench.html", "category": "State court records · operator-assisted WCCA", "status": "ready"},
    {"key": "pacer", "name": "PACER", "url": "https://pacer.login.uscourts.gov/csologin/login.jsf", "category": "Federal court records", "status": "not_implemented"},
    {"key": "sam", "name": "SAM.gov", "url": "https://sam.gov/data-services/Exclusions/Public%20V2", "category": "Federal exclusions / debarment · Download the current Public V2 extract from the link below", "status": "ready"},
    {"key": "bbb", "name": "Better Business Bureau", "url": "https://www.bbb.org/", "category": "Business complaints", "status": "ready"},
    {"key": "violation_tracker", "name": "Violation Tracker", "url": "https://violationtracker.goodjobsfirst.org/", "category": "Cross-agency enforcement evidence · free public search only", "status": "ready"},
    {"key": "wisdot", "name": "Wisconsin DOT Contractor Information", "url": "http://wisconsindot.gov/Pages/doing-bus/contractors/hcci/cntrct-info.aspx", "category": "Contractor eligibility / debarment", "status": "not_implemented"},
    {"key": "dol_enforcement", "name": "U.S. Department of Labor Enforcement Data", "url": "https://enforcedata.dol.gov/views/data_catalogs.php", "category": "Labor enforcement", "status": "not_implemented"},
    {"key": "gsa_state_debarment", "name": "GSA OIG State Suspension & Debarment Directory", "url": "https://www.gsaig.gov/content/suspension-and-debarment-sites-state", "category": "State debarment directory", "status": "not_implemented"},
    {"key": "mn_debarment", "name": "Minnesota Debarred Vendors", "url": "http://www.mmd.admin.state.mn.us/debarredreport.asp", "category": "State debarment", "status": "not_implemented"},
    {"key": "responsible_mn", "name": "Responsible Minnesota", "url": "http://responsiblemn.org/", "category": "Contractor responsibility", "status": "not_implemented"},
    {"key": "mn_pca", "name": "Minnesota PCA Enforcement Actions", "url": "https://www.pca.state.mn.us/regulations/quarterly-summary-enforcement-actions", "category": "Environmental enforcement", "status": "not_implemented"},
]

SOURCE_KEYS = {source["key"] for source in SOURCES}
