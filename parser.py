import psycopg2
from app.core.config import settings


def get_db_connection():
    return psycopg2.connect(
        dbname=settings.DB_NAME,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        host=settings.DB_HOST,
        port=settings.DB_PORT
    )


def main():
    print("🔍 Проверка дублей в базе игроков...\n")

    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. Проверка дублей по jliga_id (должно быть 0, если индекс работает)
    cursor.execute("""
        SELECT jliga_id, count(*) 
        FROM players 
        WHERE jliga_id IS NOT NULL
        GROUP BY jliga_id 
        HAVING count(*) > 1
    """)
    id_duplicates = cursor.fetchall()
    print(f"❌ Дублей по jliga_id: {len(id_duplicates)}")
    for row in id_duplicates:
        print(f"   jliga_id: {row[0]} — встречается {row[1]} раз")

    # 2. Проверка дублей по ФИО (с учетом регистра)
    cursor.execute("""
        SELECT lower(first_name), lower(last_name), count(*), array_agg(id), array_agg(coalesce(jliga_id::text, 'NULL'))
        FROM players 
        GROUP BY lower(first_name), lower(last_name) 
        HAVING count(*) > 1
        ORDER BY count(*) DESC
    """)
    name_duplicates = cursor.fetchall()

    print(f"\n❌ Групп дублей по ФИО: {len(name_duplicates)}")

    total_duplicate_records = 0

    for f_name, l_name, count, ids, j_ids in name_duplicates:
        total_duplicate_records += count
        print(f"\n   👤 '{l_name.capitalize()} {f_name.capitalize()}' — {count} записей")
        print(f"      ID в БД: {ids}")
        print(f"      jliga_id на сайте: {j_ids}")

    print(f"\n📊 ИТОГО:")
    print(f"   Всего записей, участвующих в дублях: {total_duplicate_records}")

    cursor.close()
    conn.close()


if __name__ == "__main__":
    main()