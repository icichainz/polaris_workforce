-module(polaris_tracker_ffi).
-export([now_ms/0, get_env/1]).

now_ms() ->
    erlang:system_time(millisecond).

get_env(Name) ->
    case os:getenv(binary_to_list(Name)) of
        false -> {error, nil};
        Value -> {ok, unicode:characters_to_binary(Value)}
    end.
