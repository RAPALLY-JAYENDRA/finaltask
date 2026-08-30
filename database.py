import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = "leads.db"

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        import psycopg2
        # Replace postgres:// with postgresql:// if needed (Railway sometimes provides postgres://)
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        logger.info("Connecting to PostgreSQL database...")
        return psycopg2.connect(db_url)
    else:
        # Check if running in a cloud/production environment like Railway
        is_production = os.getenv("PORT") or os.getenv("RAILWAY_STATIC_URL") or os.getenv("ENVIRONMENT") == "production"
        if is_production:
            logger.warning(
                "⚠️ CRITICAL SECURITY WARNING: Connecting to ephemeral SQLite database in production. "
                "Any leads saved will be lost when the container restarts. Please configure a DATABASE_URL "
                "or mount a Railway Persistent Volume."
            )
        sqlite_file = os.getenv("SQLITE_DB_PATH", DB_PATH)
        logger.info(f"Connecting to SQLite database at {sqlite_file}...")
        conn = sqlite3.connect(sqlite_file)
        conn.row_factory = sqlite3.Row
        return conn

def get_cursor(conn):
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        from psycopg2.extras import RealDictCursor
        return conn.cursor(cursor_factory=RealDictCursor)
    else:
        return conn.cursor()

def init_db():
    """Initializes the database schema (SQLite or PostgreSQL) if it doesn't exist."""
    db_url = os.getenv("DATABASE_URL")
    conn = get_db_connection()
    cursor = get_cursor(conn)
    
    if db_url:
        # PostgreSQL schema
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                name TEXT,
                email TEXT,
                phone TEXT,
                company TEXT,
                country TEXT,
                page_contact_form TEXT,
                interests TEXT,
                message TEXT,
                page_url TEXT,
                time TEXT,
                email_validity TEXT,
                linkedin_url TEXT,
                professional_summary TEXT,
                company_profile TEXT,
                education TEXT,
                work_experience TEXT,
                referred_product TEXT,
                use_case TEXT,
                buying_role TEXT,
                budget TEXT,
                timeline TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        # Add migration checks for postgres
        for col in ["referred_product", "use_case", "buying_role", "budget", "timeline", "company_profile", "created_at", "updated_at"]:
            try:
                cursor.execute(f"SELECT {col} FROM leads LIMIT 1")
            except Exception:
                conn.rollback() # Rollback failed transaction
                cursor = get_cursor(conn)
                type_suffix = "TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP" if "_at" in col else "TEXT"
                cursor.execute(f"ALTER TABLE leads ADD COLUMN {col} {type_suffix}")
                conn.commit()
        try:
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email ON leads (email)")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        # SQLite schema
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                email TEXT,
                phone TEXT,
                company TEXT,
                country TEXT,
                page_contact_form TEXT,
                interests TEXT,
                message TEXT,
                page_url TEXT,
                time TEXT,
                email_validity TEXT,
                linkedin_url TEXT,
                professional_summary TEXT,
                company_profile TEXT,
                education TEXT,
                work_experience TEXT,
                referred_product TEXT,
                use_case TEXT,
                buying_role TEXT,
                budget TEXT,
                timeline TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        for col in ["referred_product", "use_case", "buying_role", "budget", "timeline", "company_profile", "created_at", "updated_at"]:
            try:
                type_suffix = "TIMESTAMP DEFAULT CURRENT_TIMESTAMP" if "_at" in col else "TEXT"
                cursor.execute(f"ALTER TABLE leads ADD COLUMN {col} {type_suffix}")
                conn.commit()
            except sqlite3.OperationalError:
                pass
        try:
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email ON leads (email)")
            conn.commit()
        except sqlite3.OperationalError:
            pass
            
    conn.close()

def insert_lead(lead: dict):
    """Inserts a new enriched lead record or updates an existing Pending one in the database."""
    conn = get_db_connection()
    try:
        cursor = get_cursor(conn)
        db_url = os.getenv("DATABASE_URL")
        placeholder = "%s" if db_url else "?"
        
        name = lead.get("name")
        email = lead.get("email")
        if email:
            email = email.strip()
            if email.upper() == "N/A" or not email:
                email = None
        else:
            email = None
        
        # Only look up an existing pending record if we have a valid, non-empty email
        row = None
        if name and email and name.strip().upper() != "N/A" and email.strip().upper() != "N/A" and "@" in email:
            query_check = f"SELECT id FROM leads WHERE name = {placeholder} AND email = {placeholder} AND email_validity = {placeholder} LIMIT 1"
            cursor.execute(query_check, (name, email, "Pending"))
            row = cursor.fetchone()
        
        values = (
            lead.get("name"),
            email,
            lead.get("phone"),
            lead.get("company"),
            lead.get("country"),
            lead.get("page_contact_form"),
            lead.get("interests") or lead.get("interests_others"),
            lead.get("message"),
            lead.get("page_url"),
            lead.get("time"),
            lead.get("email_validity"),
            lead.get("linkedin_url"),
            lead.get("professional_summary"),
            lead.get("company_profile"),
            lead.get("education"),
            lead.get("work_experience"),
            lead.get("referred_product"),
            lead.get("use_case"),
            lead.get("buying_role"),
            lead.get("budget"),
            lead.get("timeline")
        )
        
        if row:
            # We found a pending record! Let's update it with the new details.
            lead_id = row["id"] if isinstance(row, dict) else row[0]
            query_update = f"""
                UPDATE leads SET
                    name = {placeholder}, email = {placeholder}, phone = {placeholder}, 
                    company = {placeholder}, country = {placeholder}, page_contact_form = {placeholder}, 
                    interests = {placeholder}, message = {placeholder}, page_url = {placeholder}, 
                    time = {placeholder}, email_validity = {placeholder}, linkedin_url = {placeholder}, 
                    professional_summary = {placeholder}, company_profile = {placeholder}, 
                    education = {placeholder}, work_experience = {placeholder}, referred_product = {placeholder}, 
                    use_case = {placeholder}, buying_role = {placeholder}, budget = {placeholder}, 
                    timeline = {placeholder}, updated_at = CURRENT_TIMESTAMP
                WHERE id = {placeholder}
            """
            cursor.execute(query_update, values + (lead_id,))
            logger.info(f"Updated existing Pending database lead ID {lead_id} with enriched profile.")
        else:
            # No existing pending record found, insert a new one.
            query_insert = f"""
                INSERT INTO leads (
                    name, email, phone, company, country, page_contact_form, 
                    interests, message, page_url, time, email_validity, 
                    linkedin_url, professional_summary, company_profile, education, work_experience,
                    referred_product, use_case, buying_role, budget, timeline
                ) VALUES ({', '.join([placeholder] * 21)})
            """
            try:
                cursor.execute(query_insert, values)
                logger.info("Inserted new lead record into database.")
            except Exception as e:
                err_str = str(e).lower()
                if email and ("unique" in err_str or "duplicate" in err_str):
                    logger.info(f"Unique violation on email '{email}'. Updating existing record instead.")
                    # Rollback current sub-transaction if needed (for PG compatibility)
                    if db_url:
                        conn.rollback()
                        cursor = get_cursor(conn)
                    cursor.execute(f"SELECT id FROM leads WHERE email = {placeholder} LIMIT 1", (email,))
                    existing_row = cursor.fetchone()
                    if existing_row:
                        lead_id = existing_row["id"] if isinstance(existing_row, dict) else existing_row[0]
                        query_update = f"""
                            UPDATE leads SET
                                name = {placeholder}, email = {placeholder}, phone = {placeholder}, 
                                company = {placeholder}, country = {placeholder}, page_contact_form = {placeholder}, 
                                interests = {placeholder}, message = {placeholder}, page_url = {placeholder}, 
                                time = {placeholder}, email_validity = {placeholder}, linkedin_url = {placeholder}, 
                                professional_summary = {placeholder}, company_profile = {placeholder}, 
                                education = {placeholder}, work_experience = {placeholder}, referred_product = {placeholder}, 
                                use_case = {placeholder}, buying_role = {placeholder}, budget = {placeholder}, 
                                timeline = {placeholder}, updated_at = CURRENT_TIMESTAMP
                            WHERE id = {placeholder}
                        """
                        cursor.execute(query_update, values + (lead_id,))
                        logger.info(f"Successfully upserted duplicate email record to lead ID {lead_id}.")
                    else:
                        raise e
                else:
                    raise e
            
        conn.commit()
        return True
    finally:
        conn.close()

def get_all_leads() -> list:
    """Retrieves all lead records from the database sorted by id desc."""
    conn = get_db_connection()
    try:
        cursor = get_cursor(conn)
        cursor.execute("SELECT * FROM leads ORDER BY id DESC")
        rows = cursor.fetchall()
        leads = []
        for row in rows:
            d = dict(row)
            if d.get("email") is None:
                d["email"] = "N/A"
            leads.append(d)
        return leads
    finally:
        conn.close()

def delete_lead(lead_id: int) -> bool:
    """Deletes a lead record by id."""
    conn = get_db_connection()
    try:
        cursor = get_cursor(conn)
        db_url = os.getenv("DATABASE_URL")
        placeholder = "%s" if db_url else "?"
        cursor.execute(f"DELETE FROM leads WHERE id = {placeholder}", (lead_id,))
        conn.commit()
        return True
    finally:
        conn.close()
