from __future__ import annotations

SOURCES = [
    {"key": "wdfi", "name": "Wisconsin Department of Financial Institutions", "url": "https://www.wdfi.org/", "category": "Business status", "status": "not_implemented"},
    {"key": "osha", "name": "OSHA Establishment Search", "url": "https://www.osha.gov/pls/imis/establishment.html", "category": "Safety / enforcement", "status": "not_implemented"},
    {"key": "wcrb", "name": "Wisconsin Compensation Rating Bureau", "url": "https://www.wcrb.org/", "category": "Workers compensation", "status": "not_implemented"},
    {"key": "wcca", "name": "Wisconsin Circuit Court Access / CCAP", "url": "https://wcca.wicourts.gov/index.xsl", "category": "State court records", "status": "not_implemented"},
    {"key": "pacer", "name": "PACER", "url": "https://pacer.login.uscourts.gov/csologin/login.jsf", "category": "Federal court records", "status": "not_implemented"},
    {"key": "sam", "name": "SAM.gov", "url": "https://sam.gov/entity-information", "category": "Federal exclusions / debarment", "status": "ready"},
    {"key": "bbb", "name": "Better Business Bureau", "url": "http://www.bbb.org/wisconsin", "category": "Business complaints", "status": "not_implemented"},
    {"key": "violation_tracker", "name": "Violation Tracker", "url": "https://violationtracker.goodjobsfirst.org/", "category": "Enforcement / violations", "status": "not_implemented"},
    {"key": "wisdot", "name": "Wisconsin DOT Contractor Information", "url": "http://wisconsindot.gov/Pages/doing-bus/contractors/hcci/cntrct-info.aspx", "category": "Contractor eligibility / debarment", "status": "not_implemented"},
    {"key": "dol_enforcement", "name": "U.S. Department of Labor Enforcement Data", "url": "https://enforcedata.dol.gov/views/data_catalogs.php", "category": "Labor enforcement", "status": "not_implemented"},
    {"key": "gsa_state_debarment", "name": "GSA OIG State Suspension & Debarment Directory", "url": "https://www.gsaig.gov/content/suspension-and-debarment-sites-state", "category": "State debarment directory", "status": "not_implemented"},
    {"key": "mn_debarment", "name": "Minnesota Debarred Vendors", "url": "http://www.mmd.admin.state.mn.us/debarredreport.asp", "category": "State debarment", "status": "not_implemented"},
    {"key": "responsible_mn", "name": "Responsible Minnesota", "url": "http://responsiblemn.org/", "category": "Contractor responsibility", "status": "not_implemented"},
    {"key": "mn_pca", "name": "Minnesota PCA Enforcement Actions", "url": "https://www.pca.state.mn.us/regulations/quarterly-summary-enforcement-actions", "category": "Environmental enforcement", "status": "not_implemented"},
]

SOURCE_KEYS = {source["key"] for source in SOURCES}
