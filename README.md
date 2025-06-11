# Crypto News Tracker

A Python application that tracks cryptocurrency news and related price data. The application fetches news from Coindesk API and price data from CoinGecko, storing the information in a PostgreSQL database.

![Screen Shot 2025-06-10 at 4 20 58 AM](https://github.com/user-attachments/assets/83d88361-d826-47ac-94e1-eecc0de7e138)
![Screen Shot 2025-06-10 at 4 21 25 AM](https://github.com/user-attachments/assets/2b359402-b94f-42e7-8434-4ff88c1bb3be)
![Screen Shot 2025-06-10 at 4 22 02 AM](https://github.com/user-attachments/assets/a1e8ba43-d91e-4eec-913c-620ba6cd9fd5)


## Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/Erm1a/crypto-news-tracker.git
   cd crypto-news-tracker
   ```

2. Set up virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On macOS/Linux
   # .venv\Scripts\activate  # On Windows
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Set up PostgreSQL database:
   - Install PostgreSQL if not already installed
   - Create a new database for the project
   - Note down the database name, user, and password

5. Configure environment variables:
   Create a `.env` file in the project root with:
   ```env
   DB_NAME=your_database_name
   DB_USER=your_database_user
   DB_PASSWORD=your_database_password
   DB_HOST=localhost
   DB_PORT=5432
   COINDESK_API_KEY=your_coindesk_api_key
   ```

6. Run the application:
   ```bash
   python main.py
   ```

The application will automatically create necessary database tables and start tracking crypto news and prices.

Open your web browser and navigate to:
```
http://localhost:5000
```

## API Endpoints

- `/api/news` - Get news articles with optional filters
- `/api/coins` - Get list of tracked coins

## License

MIT 

## Made with help of cursor <3
