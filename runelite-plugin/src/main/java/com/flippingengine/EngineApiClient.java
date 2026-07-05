package com.flippingengine;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.function.Consumer;
import javax.inject.Inject;
import javax.inject.Singleton;
import lombok.extern.slf4j.Slf4j;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

@Slf4j
@Singleton
public class EngineApiClient
{
	private static final MediaType JSON = MediaType.get("application/json; charset=utf-8");

	private final OkHttpClient httpClient;
	private final Gson gson;
	private final FlippingEngineConfig config;

	@Inject
	public EngineApiClient(OkHttpClient httpClient, Gson gson, FlippingEngineConfig config)
	{
		this.httpClient = httpClient;
		this.gson = gson;
		this.config = config;
	}

	public void requestSuggestion(JsonObject accountState,
								  Consumer<JsonObject> onSuccess,
								  Consumer<String> onError)
	{
		post("/api/suggestion", accountState, onSuccess, onError);
	}

	public void sendFills(JsonObject fills)
	{
		post("/api/fills", fills,
			r -> log.debug("fills acked"),
			e -> log.warn("failed to send fills: {}", e));
	}

	private void post(String path, JsonObject body,
					  Consumer<JsonObject> onSuccess, Consumer<String> onError)
	{
		Request request = new Request.Builder()
			.url(config.apiUrl() + path)
			.post(RequestBody.create(JSON, gson.toJson(body)))
			.build();

		httpClient.newCall(request).enqueue(new Callback()
		{
			@Override
			public void onFailure(Call call, IOException e)
			{
				onError.accept("Engine unreachable: " + e.getMessage());
			}

			@Override
			public void onResponse(Call call, Response response) throws IOException
			{
				try (Response r = response)
				{
					if (!r.isSuccessful())
					{
						onError.accept("Engine error HTTP " + r.code());
						return;
					}
					JsonObject parsed = gson.fromJson(r.body().string(), JsonObject.class);
					onSuccess.accept(parsed);
				}
				catch (Exception e)
				{
					onError.accept("Bad engine response: " + e.getMessage());
				}
			}
		});
	}
}
