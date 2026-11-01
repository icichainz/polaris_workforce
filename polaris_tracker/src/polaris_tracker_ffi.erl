-module(polaris_tracker_ffi).
-export([
    now_ms/0, get_env/1, ensure_parent_dir/1, read_file/1,
    db_prepare/2, db_run/3, db_changes/1
]).

read_file(Path) ->
    case file:read_file(Path) of
        {ok, Bits} -> {ok, Bits};
        {error, _} -> {error, nil}
    end.

now_ms() ->
    erlang:system_time(millisecond).

get_env(Name) ->
    case os:getenv(binary_to_list(Name)) of
        false -> {error, nil};
        Value -> {ok, unicode:characters_to_binary(Value)}
    end.

ensure_parent_dir(Path) ->
    case filelib:ensure_dir(unicode:characters_to_list(Path)) of
        ok -> {ok, nil};
        {error, Reason} -> {error, unicode:characters_to_binary(io_lib:format("~p", [Reason]))}
    end.

%% Prepared statements on a sqlight connection (an esqlite3 connection).
%% sqlight's own `query` prepares and finalizes the SQL on every call; these
%% keep the compiled statement and only reset, bind and step it.

db_prepare(Conn, Sql) ->
    case esqlite3:prepare(Conn, Sql, [persistent]) of
        {ok, Stmt} -> {ok, Stmt};
        {error, Code} -> db_error(Conn, Code)
    end.

db_run(Conn, Stmt, Args) ->
    _ = esqlite3:reset(Stmt),
    Result =
        case esqlite3:bind(Stmt, Args) of
            ok -> fetch(Stmt, []);
            {error, _} = Error -> Error
        end,
    _ = esqlite3:reset(Stmt),
    case Result of
        {error, Code} -> db_error(Conn, Code);
        Rows -> {ok, Rows}
    end.

db_changes(Conn) ->
    esqlite3:changes(Conn).

fetch(Stmt, Acc) ->
    case esqlite3:step(Stmt) of
        Row when is_list(Row) -> fetch(Stmt, [list_to_tuple(Row) | Acc]);
        '$done' -> lists:reverse(Acc);
        '$busy' -> {error, 5};
        {error, _} = Error -> Error
    end.

db_error(Conn, Code) when is_integer(Code) ->
    #{errmsg := Message, error_offset := Offset} = esqlite3:error_info(Conn),
    {error, {sqlight_error, sqlight:error_code_from_int(Code), Message, Offset}};
db_error(_Conn, Other) ->
    Message = unicode:characters_to_binary(io_lib:format("~p", [Other])),
    {error, {sqlight_error, sqlight:error_code_from_int(1), Message, -1}}.
