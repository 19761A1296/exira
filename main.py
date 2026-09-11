from summarizer.session_summarizer import session_summary
from database import info_database as db
from src.run_pipeline import run_pipeline
from src.router import begin_session   





def main():

    db.init_db()

    userid = input("Enter your userid: ").strip()

    if not userid:
        print("userid cannot be empty.")
        return

    # ---------- user exists: skip steps 1-4 ----------
    if db.user_exists(userid):
        print("\nUser already exists:", userid)
        print("\nuser_product_info:")
        user_product_info = db.get_product_info(userid)
        print(db.get_product_info(userid))
        print("\nuser_old_session_info:")
        user_old_session_info = db.get_past_sesion_info(userid)
        print("Line 26...",user_product_info,user_old_session_info)
        current_session_summary = begin_session(user_old_session_info, user_product_info, userid)
        session_notes =  session_summary(user_old_session_info,current_session_summary)

        
        db.update_session_info(userid,session_notes)

        
    else:
        text = input("Enter text: ")
        urls = input("Enter urls: ")
        user_product_info = run_pipeline(text,urls)
        user_old_session_info=""
        current_session_summary = begin_session(user_old_session_info, user_product_info, userid)
        session_notes =  session_summary(user_old_session_info,current_session_summary)
        db.insert_user(userid, user_product_info=user_product_info, user_session_info=session_notes)

    
    


    

    print("\nSaved user_product_info for:", userid)


if __name__ == "__main__":
    main()