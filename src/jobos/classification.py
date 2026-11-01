"""Classify a company into a coarse employer category, for coverage reporting.

This exists so the registry can report breadth across every employer segment (MNC,
enterprise SaaS, finance, GCC, IT services, product, growth, startup, remote-first)
rather than only counting boards. It is **informational**: classification never gates,
filters, or suppresses a role — a role is a role regardless of its company's category.

The signal is intentionally light and transparent:

* the ATS a board was found on is a weak prior (SmartRecruiters/Oracle/Workday skew
  enterprise; Ashby skews early-stage), and
* a curated set of name substrings pins the obvious cases (well-known MNCs, the big
  IT-services firms, major banks).

Anything the heuristic cannot place stays ``UNKNOWN`` — a neutral value, not a
judgement. Keeping it a pure function of (name, ats_type) makes it trivial to test and
cheap to run during discovery.
"""

from __future__ import annotations

import re

from jobos.models import CompanyCategory

# Curated name signals. Each maps a compiled pattern to a category. Order matters:
# the first match wins, so put the most specific/high-confidence signals first. These
# are only the unambiguous, well-known cases; the long tail stays UNKNOWN rather than
# being guessed at.
_NAME_SIGNALS: tuple[tuple[re.Pattern[str], CompanyCategory], ...] = (
    # Major IT services / engineering services / consultancies.
    (
        re.compile(
            r"\b(tcs|tata consultancy|infosys|wipro|hcl|cognizant|accenture|capgemini|"
            r"deloitte|ltimindtree|l&t infotech|mindtree|mphasis|tech mahindra|"
            r"persistent|thoughtworks|epam|globallogic|globant|nagarro|zensar|"
            r"birlasoft|coforge|hexaware|cybage|sonata|happiest minds|infocepts|"
            r"t-systems|dxc|unisys|genpact|wns|squircle|miratech|infostretch|"
            r"virtusa|cgi| ntt data|nttdata|kpmg|pwc|ey |ernst)\b",
            re.I,
        ),
        CompanyCategory.IT_SERVICES,
    ),
    # Banking, fintech and financial services.
    (
        re.compile(
            r"\b(jpmorgan|jp morgan|goldman|morgan stanley|wells fargo|visa|mastercard|"
            r"american express|amex|hsbc|barclays|deutsche bank|citi|citigroup|"
            r"bank of america|standard chartered|bny|state street|fidelity|"
            r"blackrock|paypal|stripe|razorpay|paytm|phonepe|cred|fiserv|fis |"
            r"nasdaq|experian|equifax|avaloq|revolut|wise|plaid|adyen)\b",
            re.I,
        ),
        CompanyCategory.FINANCE,
    ),
    # Large multinational technology companies.
    (
        re.compile(
            r"\b(google|alphabet|microsoft|amazon|aws|apple|meta|facebook|netflix|"
            r"nvidia|intel|qualcomm|cisco|ibm|oracle|sap|dell|vmware|hp |hpe|"
            r"samsung|huawei|sony|lenovo|broadcom|arm |texas instruments|"
            r"micron|western digital|seagate|renesas|nxp)\b",
            re.I,
        ),
        CompanyCategory.MNC_TECH,
    ),
    # Global Capability Centres: in-house tech arms of non-tech multinationals.
    (
        re.compile(
            r"\b(walmart|target|lowe'?s|optum|unitedhealth|wells fargo|honeywell|"
            r"shell|chevron|boeing|caterpillar|john deere|3m|ge |general electric|"
            r"thomson reuters|nielsen|nielseniq|mckinsey|bosch|siemens|philips|"
            r"airbus|rolls.royce|mercedes|bmw|volvo|zf |delta|american airlines|"
            r"pepsico|unilever|procter|colgate|maersk|schneider)\b",
            re.I,
        ),
        CompanyCategory.GCC,
    ),
    # Publicly traded / mature enterprise SaaS.
    (
        re.compile(
            r"\b(salesforce|servicenow|adobe|workday|atlassian|sap |splunk|datadog|"
            r"snowflake|mongodb|confluent|hashicorp|elastic|okta|zscaler|crowdstrike|"
            r"palo alto|vmware|citrix|twilio|zendesk|freshworks|zoho|sprinklr|"
            r"intuit|autodesk|ansys|ptc|sas |teradata|cloudera)\b",
            re.I,
        ),
        CompanyCategory.ENTERPRISE_SAAS,
    ),
)

# The ATS a board lives on is a weak prior for the long tail that no name signal hits.
# These skews are real (enterprise buyers vs. VC-backed startups) but soft, so they
# only apply when the name is unrecognised.
_ATS_PRIOR: dict[str, CompanyCategory] = {
    "workday": CompanyCategory.ENTERPRISE_SAAS,
    "oracle": CompanyCategory.GCC,
    "smartrecruiters": CompanyCategory.GROWTH,
    "ashby": CompanyCategory.STARTUP,
    "lever": CompanyCategory.GROWTH,
    "greenhouse": CompanyCategory.GROWTH,
}


def classify_company(
    name: str, ats_type: str = "", *, remote_first: bool = False
) -> CompanyCategory:
    """Best-effort employer category from the company name and its ATS.

    ``remote_first`` lets the caller promote a board whose roles are overwhelmingly
    remote-India to ``REMOTE_FIRST`` when no stronger signal applies. The function is
    pure and side-effect free.
    """
    text = f" {name.casefold()} "
    for pattern, category in _NAME_SIGNALS:
        if pattern.search(text):
            return category
    if remote_first:
        return CompanyCategory.REMOTE_FIRST
    return _ATS_PRIOR.get(ats_type, CompanyCategory.UNKNOWN)


# Well-known major employers, by category, used to report which big names are still
# missing from the registry (requirement 9). Kept small and recognisable rather than
# exhaustive — this is a "are the obvious ones covered?" checklist.
# Known Global Capability Centre parents — non-tech-native multinationals (banks,
# retailers, pharma, auto, industrial, insurers) that run large in-house engineering
# centres in India. Matched on a name substring. This is the evidence basis for
# tagging a board as a GCC without scraping gated GCC directories: if the employer
# name matches one of these well-known parents, it is a GCC with high confidence.
GCC_PARENTS: tuple[str, ...] = (
    # Banking / financial services
    "wells fargo", "jpmorgan", "jp morgan", "goldman", "morgan stanley", "citi",
    "bank of america", "barclays", "hsbc", "deutsche bank", "standard chartered",
    "bny", "state street", "lloyds", "natwest", "ubs", "credit suisse", "mufg",
    "wells", "fidelity", "blackrock", "mastercard", "visa", "american express",
    "amex", "capital one", "discover", "synchrony", "svb", "northern trust",
    "nomura", "societe generale", "bnp paribas", "wells fargo", "schwab",
    # Retail / consumer / e-commerce
    "walmart", "target", "lowe", "tesco", "ikea", "nike", "albertsons", "kroger",
    "best buy", "gap", "macy", "williams-sonoma", "wayfair", "ebay", "etsy",
    # Healthcare / pharma / life sciences
    "optum", "unitedhealth", "cvs", "cigna", "anthem", "elevance", "humana",
    "pfizer", "merck", "novartis", "roche", "astrazeneca", "gsk", "sanofi",
    "bristol", "amgen", "eli lilly", "abbvie", "boehringer", "bayer", "takeda",
    "baxter", "stryker", "medtronic", "becton", "edwards lifesciences",
    # Industrial / automotive / energy
    "bosch", "siemens", "ge ", "general electric", "honeywell", "3m", "caterpillar",
    "john deere", "cummins", "emerson", "schneider", "abb", "rockwell", "eaton",
    "parker", "dow", "dupont", "shell", "chevron", "bp ", "exxon", "aramco",
    "mercedes", "bmw", "volkswagen", "volvo", "toyota", "ford", "general motors",
    "continental", "zf ", "aptiv", "magna", "valeo", "cummins", "rolls-royce",
    "boeing", "airbus", "collins aerospace", "raytheon", "lockheed",
    # Insurance / telecom / media / logistics / other
    "allstate", "progressive", "metlife", "prudential", "aig", "aon", "marsh",
    "verizon", "at&t", "vodafone", "telstra", "comcast", "disney", "thomson reuters",
    "nielsen", "maersk", "fedex", "ups", "dhl", "expedia", "booking", "priceline",
    "mckinsey", "accenture", "deloitte", "pwc", "kpmg", "ey ", "ernst",
    "philips", "unilever", "procter", "colgate", "pepsico", "coca-cola", "nestle",
    "experian", "equifax", "transunion", "s&p", "moody", "gartner", "clarivate",
)


def is_gcc(name: str) -> bool:
    """True when the employer name matches a known GCC parent (evidence-backed).

    Deliberately conservative: it only returns True for recognised multinational
    parents that are well-documented as running India capability centres, so a GCC
    tag is defensible rather than guessed. Unknown names are not GCCs by default.
    """
    text = f" {name.casefold()} "
    return any(parent in text for parent in GCC_PARENTS)


MAJOR_EMPLOYERS: dict[CompanyCategory, tuple[str, ...]] = {
    CompanyCategory.MNC_TECH: (
        "Google", "Microsoft", "Amazon", "Apple", "Nvidia", "Qualcomm", "Cisco",
        "IBM", "Oracle", "SAP", "Intel", "Dell", "VMware", "Samsung",
    ),
    CompanyCategory.FINANCE: (
        "JPMorgan", "Goldman Sachs", "Morgan Stanley", "Wells Fargo", "Visa",
        "American Express", "HSBC", "Barclays", "Citi", "PhonePe", "Paytm",
    ),
    CompanyCategory.GCC: (
        "Walmart", "Target", "Optum", "UnitedHealth", "Honeywell", "Shell",
        "Thomson Reuters", "Nielsen", "Bosch", "Siemens",
    ),
    CompanyCategory.IT_SERVICES: (
        "TCS", "Infosys", "Wipro", "HCL", "Cognizant", "Accenture", "Capgemini",
        "LTIMindtree", "Tech Mahindra", "Persistent", "ThoughtWorks", "EPAM",
    ),
    CompanyCategory.ENTERPRISE_SAAS: (
        "Salesforce", "ServiceNow", "Adobe", "Atlassian", "SAP",
    ),
    CompanyCategory.PRODUCT: (
        "Flipkart", "Zoho", "Zomato", "Swiggy", "Dream11",
    ),
}
